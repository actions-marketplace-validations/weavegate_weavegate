package timing

import (
	"context"
	"sync"
	"testing"
)

func TestNestedConcurrentIntervals(t *testing.T) {
	operation, cancel := context.WithCancel(context.Background())
	ctx, recorder := New(operation)
	ctx, finishRoot := Start(ctx, "command")
	ctx, finishRun := StartSchedule(ctx, "sch_example")
	cancel()
	if ctx.Err() != context.Canceled {
		t.Fatal("recording lost cancellation")
	}
	// Detached cleanup retains correlation without inheriting cancellation.
	cleanup := context.WithoutCancel(ctx)
	if cleanup.Err() != nil {
		t.Fatal("cleanup inherited cancellation")
	}
	var joined sync.WaitGroup
	for range 20 {
		joined.Add(1)
		go func() {
			defer joined.Done()
			_, finish := Start(cleanup, "adapter_stop")
			finish()
			finish()
		}()
	}
	joined.Wait()
	finishRun()
	finishRoot()
	spans := recorder.Snapshot()
	if len(spans) != 22 || spans[1].ScheduleID != "sch_example" {
		t.Fatalf("intervals = %+v", spans)
	}
	for index, span := range spans {
		if span.ID != index+1 || span.StartNS < 0 || span.DurationNS < 0 {
			t.Fatalf("invalid interval: %+v", span)
		}
		if index == 0 {
			if span.ParentID != 0 {
				t.Fatal("root has a parent")
			}
			continue
		}
		parent := spans[span.ParentID-1]
		if span.StartNS < parent.StartNS || span.StartNS+span.DurationNS > parent.StartNS+parent.DurationNS {
			t.Fatalf("child outside parent: child=%+v parent=%+v", span, parent)
		}
		if index > 1 && span.ParentID != 2 {
			t.Fatalf("cleanup parent = %d, want schedule interval", span.ParentID)
		}
	}
	spans[0].Name = "mutated"
	if recorder.Snapshot()[0].Name != "command" {
		t.Fatal("snapshot aliases the recorder")
	}
}

func TestDisabledRecordingPreservesContext(t *testing.T) {
	for _, ctx := range []context.Context{nil, context.Background()} {
		got, finish := Start(ctx, "unused")
		if got != ctx {
			t.Fatal("disabled recorder changed context")
		}
		finish()
	}
}
