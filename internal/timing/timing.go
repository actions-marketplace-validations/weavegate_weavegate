// Package timing records local monotonic intervals as volatile metadata.
// It never participates in synchronization, deadlines, or verdicts.
package timing

import (
	"context"
	"sync"
	"time"
)

// Span is an interval on one recorder's monotonic clock. ParentID is zero
// for a root interval; children may overlap and must not be summed as wall time.
type Span struct {
	ID         int
	ParentID   int
	Name       string
	ScheduleID string
	StartNS    int64
	DurationNS int64
}

type Recorder struct {
	mu     sync.Mutex
	origin time.Time
	spans  []Span
}

type scope struct {
	recorder *Recorder
	parentID int
}

type scopeKey struct{}

// New enables recording on a context without changing its cancellation policy.
func New(ctx context.Context) (context.Context, *Recorder) {
	r := &Recorder{origin: time.Now()}
	return context.WithValue(ctx, scopeKey{}, scope{recorder: r}), r
}

// Start opens a child interval. Without a recorder it is a no-op, including on
// a nil context, so instrumentation does not change existing validation paths.
// The returned finish function is safe to call more than once.
func Start(ctx context.Context, name string) (context.Context, func()) {
	return start(ctx, name, "")
}

// StartSchedule identifies every execution, including passing exploration
// candidates and repeated replays, without retaining their normalized evidence.
func StartSchedule(ctx context.Context, id string) (context.Context, func()) {
	return start(ctx, "schedule_run", id)
}

func start(ctx context.Context, name, scheduleID string) (context.Context, func()) {
	if ctx == nil {
		return ctx, func() {}
	}
	s, ok := ctx.Value(scopeKey{}).(scope)
	if !ok {
		return ctx, func() {}
	}
	r := s.recorder
	r.mu.Lock()
	id := len(r.spans) + 1
	started := time.Now()
	r.spans = append(r.spans, Span{
		ID: id, ParentID: s.parentID, Name: name, ScheduleID: scheduleID,
		StartNS: started.Sub(r.origin).Nanoseconds(),
	})
	r.mu.Unlock()
	var once sync.Once
	finish := func() {
		once.Do(func() {
			elapsed := time.Since(started).Nanoseconds()
			r.mu.Lock()
			r.spans[id-1].DurationNS = elapsed
			r.mu.Unlock()
		})
	}
	return context.WithValue(ctx, scopeKey{}, scope{recorder: r, parentID: id}), finish
}

// Snapshot copies the intervals in start order. Call after all measured work
// has joined; the recorder is observation and does not wait for that work.
func (r *Recorder) Snapshot() []Span {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]Span(nil), r.spans...)
}
