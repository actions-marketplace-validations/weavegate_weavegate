package orchestrator

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"time"

	"github.com/weavegate/weavegate/internal/oracle"
	"github.com/weavegate/weavegate/internal/scenario"
	"github.com/weavegate/weavegate/internal/sut"
	"github.com/weavegate/weavegate/internal/timing"
)

// CandidateSummary records stable evidence for one successfully evaluated
// candidate without retaining its full run result.
type CandidateSummary struct {
	Index       int
	ScheduleID  string
	Violations  int
	Fingerprint string
}

// ExploreResult summarizes an ordered strategy traversal. Candidates is
// meaningful only when CandidatesKnown is true.
type ExploreResult struct {
	Candidates      int
	CandidatesKnown bool
	Evaluated       int
	ViolatingIndex  int
	Violating       scenario.Schedule
	ViolatingRun    RunResult
	Summaries       []CandidateSummary
	Exhausted       bool
	Elapsed         time.Duration
}

// Explore runs strategy candidates in order and stops at the first Oracle
// violation. Run and Oracle failures remain errors and include partial progress.
func (o *Orchestrator) Explore(
	ctx context.Context,
	value scenario.Scenario,
	strategy scenario.Strategy,
	evaluator oracle.Evaluator,
) (ExploreResult, error) {
	if ctx == nil {
		return ExploreResult{}, errors.New("explore schedules: context is required")
	}
	if isNilStrategy(strategy) {
		return ExploreResult{}, errors.New("explore schedules: strategy is required")
	}
	if err := ctx.Err(); err != nil {
		return ExploreResult{}, fmt.Errorf("explore schedules: %w", err)
	}

	plan, err := strategy.Schedules(value.Clone())
	if err != nil {
		return ExploreResult{}, fmt.Errorf("explore schedules: build candidate plan: %w", err)
	}
	return o.ExplorePlan(ctx, value, plan, evaluator)
}

// ExplorePlan traverses a candidate plan that was already built and validated
// before external fixture provisioning. This boundary lets CLI callers prove
// that candidate-count and strategy failures cannot occur after a container
// starts. SchedulePlan.Seq must be repeatable when the same plan is used for
// multiple exploration passes.
func (o *Orchestrator) ExplorePlan(
	ctx context.Context,
	value scenario.Scenario,
	plan scenario.SchedulePlan,
	evaluator oracle.Evaluator,
) (result ExploreResult, returnErr error) {
	ctx, finish := timing.Start(ctx, "explore_pass")
	defer finish()
	startedAt := time.Now()
	defer func() {
		result.Elapsed = time.Since(startedAt)
		result = cloneExploreResult(result)
	}()

	if ctx == nil {
		return result, errors.New("explore schedules: context is required")
	}
	if isNilEvaluator(evaluator) {
		return result, errors.New("explore schedules: Oracle evaluator is required")
	}
	if err := ctx.Err(); err != nil {
		return result, fmt.Errorf("explore schedules: %w", err)
	}
	if plan.TotalKnown && plan.Total < 0 {
		return result, fmt.Errorf("explore schedules: candidate total must not be negative: %d", plan.Total)
	}
	if plan.Seq == nil {
		return result, errors.New("explore schedules: candidate sequence is required")
	}

	result.CandidatesKnown = plan.TotalKnown
	if plan.TotalKnown {
		result.Candidates = plan.Total
	}

	for schedule := range plan.Seq {
		index := result.Evaluated + 1
		if result.CandidatesKnown && index > result.Candidates {
			return result, fmt.Errorf(
				"explore schedules: strategy yielded candidate %d beyond declared total %d",
				index,
				result.Candidates,
			)
		}

		result.Evaluated = index
		run, err := o.Run(ctx, value, schedule, evaluator)
		if err != nil {
			return result, wrapExploreCandidateError(result, index, err)
		}
		if len(run.Evaluation.Results) == 0 {
			return result, wrapExploreCandidateError(
				result,
				index,
				errors.New("evaluation contains no Oracle results"),
			)
		}

		violations := evaluationViolationCount(run.Evaluation)
		result.Summaries = append(result.Summaries, CandidateSummary{
			Index:       index,
			ScheduleID:  schedule.ID,
			Violations:  violations,
			Fingerprint: run.Fingerprint,
		})
		if violations == 0 {
			continue
		}

		result.ViolatingIndex = index
		result.Violating = schedule.Clone()
		result.ViolatingRun = cloneRunResult(run)
		return result, nil
	}

	if result.Evaluated == 0 {
		return result, errors.New("explore schedules: strategy yielded no candidates")
	}
	if result.CandidatesKnown && result.Evaluated != result.Candidates {
		return result, fmt.Errorf(
			"explore schedules: strategy yielded %d candidates, declared %d",
			result.Evaluated,
			result.Candidates,
		)
	}

	result.Exhausted = true
	return result, nil
}

func isNilStrategy(strategy scenario.Strategy) bool {
	if strategy == nil {
		return true
	}
	value := reflect.ValueOf(strategy)
	switch value.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map,
		reflect.Pointer, reflect.Slice:
		return value.IsNil()
	default:
		return false
	}
}

func wrapExploreCandidateError(result ExploreResult, index int, err error) error {
	if result.CandidatesKnown {
		return fmt.Errorf("explore candidate %d/%d: %w", index, result.Candidates, err)
	}
	return fmt.Errorf("explore candidate %d: %w", index, err)
}

func evaluationViolationCount(evaluation oracle.Evaluation) int {
	violations := 0
	for _, result := range evaluation.Results {
		violations += len(result.Violations)
	}
	return violations
}

func cloneExploreResult(value ExploreResult) ExploreResult {
	cloned := value
	cloned.Summaries = append([]CandidateSummary(nil), value.Summaries...)
	cloned.Violating = value.Violating.Clone()
	cloned.ViolatingRun = cloneRunResult(value.ViolatingRun)
	return cloned
}

func cloneRunResult(value RunResult) RunResult {
	cloned := value
	if value.Workers != nil {
		cloned.Workers = append([]sut.WorkerResult(nil), value.Workers...)
	}
	if value.Unstarted != nil {
		cloned.Unstarted = append([]sut.UnstartedResult(nil), value.Unstarted...)
	}
	if value.Terminals != nil {
		cloned.Terminals = value.Terminals.Clone()
	}
	if value.Trace != nil {
		cloned.Trace = value.Trace.Clone()
	}
	cloned.Evaluation = cloneEvaluation(value.Evaluation)
	return cloned
}

func cloneEvaluation(value oracle.Evaluation) oracle.Evaluation {
	cloned := oracle.Evaluation{Fingerprint: value.Fingerprint}
	if value.Results == nil {
		return cloned
	}

	cloned.Results = make([]oracle.OracleResult, len(value.Results))
	for resultIndex, result := range value.Results {
		cloned.Results[resultIndex] = oracle.OracleResult{OracleID: result.OracleID}
		if result.Violations == nil {
			continue
		}
		cloned.Results[resultIndex].Violations = make([]oracle.Violation, len(result.Violations))
		for violationIndex, violation := range result.Violations {
			clonedViolation := oracle.Violation{
				OracleID: violation.OracleID,
				Kind:     violation.Kind,
			}
			if violation.Rows != nil {
				clonedViolation.Rows = make([]oracle.Row, len(violation.Rows))
				for rowIndex, row := range violation.Rows {
					if row == nil {
						continue
					}
					clonedRow := make(oracle.Row, len(row))
					for key, rowValue := range row {
						clonedRow[key] = rowValue
					}
					clonedViolation.Rows[rowIndex] = clonedRow
				}
			}
			cloned.Results[resultIndex].Violations[violationIndex] = clonedViolation
		}
	}
	return cloned
}
