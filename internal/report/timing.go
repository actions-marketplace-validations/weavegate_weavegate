package report

import "github.com/weavegate/weavegate/internal/timing"

// Timings is optional volatile metadata captured before artifact writing.
// Its report-owned shape is independent of the engine's internal span model.
type Timings struct {
	Scope string       `json:"scope"`
	Spans []TimingSpan `json:"spans"`
}

type TimingSpan struct {
	ID         int    `json:"id"`
	ParentID   int    `json:"parent_id"`
	Name       string `json:"name"`
	ScheduleID string `json:"schedule_id,omitempty"`
	StartNS    int64  `json:"start_ns"`
	DurationNS int64  `json:"duration_ns"`
}

func NewTimings(spans []timing.Span) *Timings {
	converted := make([]TimingSpan, 0, len(spans))
	for _, span := range spans {
		converted = append(converted, TimingSpan{
			ID: span.ID, ParentID: span.ParentID, Name: span.Name,
			ScheduleID: span.ScheduleID, StartNS: span.StartNS, DurationNS: span.DurationNS,
		})
	}
	return &Timings{Scope: "before_artifact_write", Spans: converted}
}
