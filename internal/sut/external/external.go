// Package external implements the engine peer of external SUT wire v1 using
// one owned child JVM per adapter. The CLI selects it through configuration;
// this package never resets a fixture.
package external

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"reflect"
	"slices"
	"strconv"
	"sync"
	"time"

	"github.com/go-sql-driver/mysql"
	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/sut"
	"github.com/weavegate/weavegate/internal/syncpoint"
)

// Options describes a prebuilt child and its declared registration. The child
// must not spawn descendants. Budgets bound supervision, never coordination.
// RunID may correlate several adapters; each adapter generates a fresh session.
type Options struct {
	Java           string
	JAR            string
	RunID          string
	Commands       []string
	Points         []string
	Capacity       int
	StartupTimeout time.Duration
	CancelTimeout  time.Duration
	StopTimeout    time.Duration
}

type invocation struct {
	id, worker        string
	parent, ctx       context.Context
	cancel            context.CancelFunc
	reason            string
	cause             error
	accepted, sending bool
	delivered         chan struct{}
	arrival           int
	outstanding       bool
	bridges           int
	terminal          map[string]any
	terminalAt        time.Time
	started           time.Time
	results           chan sut.InvocationOutcome
	done              chan struct{}
	retired           bool
}

type outbound struct {
	frame  frame
	worker *invocation
}

type adapter struct {
	mu     sync.Mutex
	opts   Options
	client syncpoint.Client
	launch func(string, string) (*child, error)
	id     func() (string, error)
	now    func() time.Time
	// beforeRelease is a test barrier outside the serialization lock. It cannot
	// decide cancellation, enqueue a frame, or complete an invocation.
	beforeRelease                                                    func()
	beforeWrite                                                      func(string)
	faults                                                           sut.FaultLatch
	started, ready, stopping, stopped, startSent, stopSent           bool
	run, session                                                     string
	startDeadline, stopDeadline, graceDeadline                       time.Time
	proc                                                             *child
	launchDone, readyDone, readDone, writeDone, stderrDone, exitDone chan struct{}
	stopDone                                                         chan struct{}
	startDelivered                                                   chan struct{}
	stopDelivered                                                    chan struct{}
	stopErr, exitErr                                                 error
	eof                                                              bool
	queue                                                            chan outbound
	writerCancel                                                     context.CancelFunc
	seq, received                                                    int
	digests                                                          map[int][32]byte
	invocations                                                      map[string]*invocation
	workers                                                          map[string]*invocation
	stale                                                            uint64
	logs                                                             tail
}

var _ sut.Adapter = (*adapter)(nil)
var _ sut.Handle = (*adapter)(nil)

// New validates immutable launch settings. Construct a new adapter with the
// schedule's Client for every execution, including repeats and exploration.
func New(opts Options, client syncpoint.Client) (sut.Adapter, error) {
	if client == nil || opts.Java == "" || opts.JAR == "" || opts.Capacity < 1 || opts.Capacity > 1024 || len(opts.Commands) == 0 {
		return nil, errors.New("invalid external SUT launch settings")
	}
	for _, budget := range []time.Duration{opts.StartupTimeout, opts.CancelTimeout, opts.StopTimeout} {
		if budget < time.Millisecond || budget.Milliseconds() > 2147483647 {
			return nil, errors.New("invalid external SUT budget")
		}
	}
	for _, list := range [][]string{opts.Commands, opts.Points} {
		seen := map[string]bool{}
		for _, s := range list {
			if !name(s) || seen[s] {
				return nil, errors.New("invalid external SUT registration")
			}
			seen[s] = true
		}
	}
	if opts.RunID != "" && !identity(opts.RunID) {
		return nil, errors.New("invalid external SUT run identity")
	}
	opts.Commands = append([]string{}, opts.Commands...)
	opts.Points = append([]string{}, opts.Points...)
	return &adapter{opts: opts, client: client, launch: launchJVM, id: randomID, now: time.Now,
		launchDone: make(chan struct{}), readyDone: make(chan struct{}), readDone: make(chan struct{}),
		writeDone: make(chan struct{}), stderrDone: make(chan struct{}), exitDone: make(chan struct{}),
		stopDone: make(chan struct{}), startDelivered: make(chan struct{}), stopDelivered: make(chan struct{}), queue: make(chan outbound, 2048),
		digests: map[int][32]byte{}, invocations: map[string]*invocation{}, workers: map[string]*invocation{},
	}, nil
}

func randomID() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", errors.New("external SUT identity generation failed")
	}
	return hex.EncodeToString(b[:]), nil
}

func (a *adapter) Faults() sut.SessionFaults { return &a.faults }

func (a *adapter) Start(ctx context.Context, cfg sut.SUTConfig, db *fixture.DB) (sut.Handle, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if db == nil {
		return nil, errors.New("external SUT requires a fixture connection descriptor")
	}
	password, err := db.Connection.Password()
	if err != nil {
		return nil, err
	}
	d := db.Connection
	body := map[string]any{"variant": cfg.Variant, "params": cfg.Params, "commands": a.opts.Commands, "points": a.opts.Points,
		"capacity": a.opts.Capacity, "startup_ms": 1, "cancel_ms": a.opts.CancelTimeout.Milliseconds(),
		"database": map[string]any{"driver": d.Driver, "host": d.Host, "port": d.Port, "name": d.Name, "username": d.Username, "password": password}}
	if cfg.Params == nil {
		body["params"] = map[string]string{}
	}
	return a.start(ctx, body)
}

// start takes ownership of the private start body and discards its credential
// fields after the sole transport write. It is also the scripted-peer test seam.
func (a *adapter) start(ctx context.Context, body map[string]any) (sut.Handle, error) {
	a.mu.Lock()
	if a.started || a.stopping {
		a.mu.Unlock()
		return nil, errors.New("external SUT adapter is single use")
	}
	a.started = true
	a.startDeadline = a.now().Add(a.opts.StartupTimeout)
	if deadline, ok := ctx.Deadline(); ok && deadline.Before(a.startDeadline) {
		a.startDeadline = deadline
	}
	run, session, err := a.identities()
	if err == nil && !validOutboundUTF8(body) {
		err = errProtocol
	}
	if err == nil {
		a.run, a.session = run, session
		raw, marshalErr := json.Marshal(frame{V: 1, Type: "start", Run: run, Session: session, Seq: 1, Body: body})
		if marshalErr != nil {
			err = errProtocol
		} else if _, decodeErr := decodeFrame(raw); decodeErr != nil {
			err = decodeErr
		}
	}
	if err != nil {
		close(a.launchDone)
		a.failLocked(err, "startup", false)
		a.mu.Unlock()
		return nil, a.startFailure(err)
	}
	a.mu.Unlock()
	p, err := a.launch(a.opts.Java, a.opts.JAR)
	a.mu.Lock()
	a.proc = p
	if err == nil {
		writerCtx, cancel := context.WithCancel(context.Background())
		a.writerCancel = cancel
		go a.writeLoop(writerCtx)
		go a.readLoop()
		go a.watchExit()
		// Log read errors cannot authorize protocol completion; stdout and Wait
		// remain authoritative. Never publish raw stderr errors.
		go func() { _, _ = io.Copy(&a.logs, p.stderr); close(a.stderrDone) }()
		if !a.stopping {
			a.enqueueLocked("start", body, nil)
		}
	} else {
		a.failLocked(errTransport, "startup", false)
	}
	close(a.launchDone)
	deadline := a.startDeadline
	a.mu.Unlock()
	if err != nil {
		return nil, a.startFailure(errTransport)
	}
	timer := time.NewTimer(time.Until(deadline))
	defer timer.Stop()
	select {
	case <-a.readyDone:
		a.mu.Lock()
		ok := a.ready && !a.stopping && a.faults.Err() == nil && ctx.Err() == nil && a.now().Before(a.startDeadline)
		a.mu.Unlock()
		if ok {
			return a, nil
		}
	case <-a.faults.Done():
	case <-a.stopDone:
	case <-ctx.Done():
	case <-timer.C:
	}
	a.mu.Lock()
	if ctx.Err() != context.Canceled && !a.stopping && !a.now().Before(a.startDeadline) {
		// An exhausted startup deadline is a session failure, even if the
		// peer later claims normal cleanup. Explicit cancellation/Stop still
		// permits the pre-ready stopped path.
		a.failLocked(context.DeadlineExceeded, "startup", true)
	}
	a.mu.Unlock()
	err = ctx.Err()
	if err == nil {
		if f := a.faults.Err(); f != nil {
			err = f
		} else {
			err = errors.New("external SUT startup did not complete")
		}
	}
	// Stop during startup is a valid cleanup path. Context cancellation alone
	// must not send fatal and thereby forbid the peer's stopped acknowledgement.
	return nil, a.startFailure(err)
}

func (a *adapter) identities() (string, string, error) {
	run := a.opts.RunID
	var err error
	if run == "" {
		run, err = a.id()
	}
	if err != nil {
		return "", "", err
	}
	session, err := a.id()
	if err != nil || !identity(run) || !identity(session) {
		return "", "", errors.New("external SUT identity generation failed")
	}
	return run, session, nil
}

func (a *adapter) startFailure(cause error) error {
	ctx, cancel := context.WithTimeout(context.Background(), a.opts.StopTimeout)
	defer cancel()
	return errors.Join(cause, a.Stop(ctx))
}

func (a *adapter) Invoke(ctx context.Context, worker, command string) (<-chan sut.InvocationOutcome, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if !a.ready || a.stopping {
		return nil, errors.New("external SUT admission is closed")
	}
	if f := a.faults.Err(); f != nil {
		return nil, f
	}
	if !name(worker) || !slices.Contains(a.opts.Commands, command) {
		return nil, errors.New("external SUT unknown command or invalid worker")
	}
	if a.workers[worker] != nil || len(a.workers) >= a.opts.Capacity {
		return nil, errors.New("external SUT worker or capacity is reserved")
	}
	id, err := a.id()
	if err != nil || !identity(id) || a.invocations[id] != nil {
		a.failLocked(errProtocol, "protocol", true)
		return nil, a.faults.Err()
	}
	bridgeCtx, cancel := context.WithCancel(context.WithoutCancel(ctx))
	w := &invocation{id: id, worker: worker, parent: ctx, ctx: bridgeCtx, cancel: cancel,
		delivered: make(chan struct{}), started: a.now(), results: make(chan sut.InvocationOutcome, 1), done: make(chan struct{})}
	a.workers[worker], a.invocations[id] = w, w
	a.enqueueLocked("invoke", map[string]any{"invocation": id, "worker": worker, "command": command}, w)
	go func() {
		select {
		case <-ctx.Done():
			a.mu.Lock()
			a.cancelLocked(w, "context")
			a.mu.Unlock()
		case <-w.done:
		}
	}()
	return w.results, nil
}

func (a *adapter) cancelLocked(w *invocation, reason string) {
	if w.retired || w.reason != "" {
		return
	}
	if w.parent.Err() != nil {
		reason = "context"
	}
	w.reason = reason
	w.cause = context.Canceled
	if reason == "context" && w.parent.Err() != nil {
		w.cause = errors.Join(w.parent.Err(), context.Cause(w.parent))
	}
	w.cancel()
	w.outstanding = false
	if a.faults.Err() == nil {
		a.enqueueLocked("cancel", map[string]any{"invocation": w.id, "worker": w.worker, "reason": reason}, nil)
	}
}

// All queues are appended under mu, including cancellation and release. No
// serialization lock is held across pipe I/O or a blocking queue operation.
func (a *adapter) enqueueLocked(kind string, body map[string]any, w *invocation) bool {
	if a.seq == maxSequence {
		a.failLocked(errProtocol, "protocol", false)
		return false
	}
	f := frame{V: 1, Type: kind, Run: a.run, Session: a.session, Seq: a.seq + 1, Body: body}
	select {
	case a.queue <- outbound{frame: f, worker: w}:
		a.seq++
		return true
	default:
		a.failLocked(errors.New("external SUT control queue exhausted"), "transport", false)
		return false
	}
}

func (a *adapter) failLocked(cause error, kind string, send bool) {
	if a.faults.Err() != nil {
		return
	}
	a.faults.Fail(cause)
	for _, w := range a.workers {
		w.cancel()
		w.outstanding = false
		if w.bridges == 0 {
			a.finishLocked(w)
		}
	}
	if send && a.startSent {
		message := "external SUT session failed"
		var failure *wireFailure
		if errors.As(cause, &failure) {
			message = failure.message
		}
		a.enqueueLocked("fatal", map[string]any{"kind": kind, "message": message}, nil)
	}
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), a.opts.StopTimeout)
		defer cancel()
		_ = a.Stop(ctx) // The original fault and shared Stop result remain latched.
	}()
}

// wireFailure messages are closed, locally authored summaries. Peer-provided
// strings never enter this type or an outbound diagnostic.
type wireFailure struct{ message string }

func (e *wireFailure) Error() string { return "external SUT protocol violation: " + e.message }
func (e *wireFailure) Unwrap() error { return errProtocol }
func (a *adapter) rejectLocked(message, kind string) bool {
	a.failLocked(&wireFailure{message: message}, kind, true)
	return false
}

func (a *adapter) writeLoop(ctx context.Context) {
	defer close(a.writeDone)
	defer func() {
		// Startup may be canceled before its queued frame reaches the writer.
		// Release credentials in abandoned control messages on every exit path.
		for {
			select {
			case out := <-a.queue:
				if out.frame.Type == "start" {
					clear(out.frame.Body)
				}
			default:
				return
			}
		}
	}()
	for {
		select {
		case <-ctx.Done():
			return
		case out := <-a.queue:
			if out.frame.Type == "start" {
				// Also covers an exhausted budget or Stop winning before the write.
				defer clear(out.frame.Body)
			}
			if a.beforeWrite != nil {
				a.beforeWrite(out.frame.Type)
			}
			a.mu.Lock()
			if ctx.Err() != nil || (out.frame.Type == "start" && a.stopping) {
				a.mu.Unlock()
				return
			}
			if out.frame.Type == "start" || out.frame.Type == "stop" {
				deadline, field := a.startDeadline, "startup_ms"
				if out.frame.Type == "stop" {
					deadline, field = a.graceDeadline, "budget_ms"
				}
				ms := deadline.Sub(a.now()).Milliseconds()
				if ms < 1 {
					a.failLocked(context.DeadlineExceeded, "shutdown", false)
					a.mu.Unlock()
					return
				}
				out.frame.Body[field] = ms
			}
			if out.frame.Type == "start" {
				a.startSent = true
			}
			if out.frame.Type == "stop" {
				a.stopSent = true
			}
			if out.worker != nil {
				out.worker.sending = true
			}
			a.mu.Unlock()
			err := writeFrame(a.proc.stdin, out.frame)
			if out.frame.Type == "start" {
				clear(out.frame.Body)
			}
			a.mu.Lock()
			if err != nil {
				a.failLocked(err, "transport", false)
			}
			if out.worker != nil {
				close(out.worker.delivered)
			}
			if out.frame.Type == "start" {
				close(a.startDelivered)
			}
			if out.frame.Type == "stop" {
				close(a.stopDelivered)
			}
			a.mu.Unlock()
			if err != nil {
				return
			}
		}
	}
}

func (a *adapter) readLoop() {
	defer close(a.readDone)
	for {
		f, raw, err := readFrame(a.proc.stdout)
		a.mu.Lock()
		if err != nil {
			if err == io.EOF && a.stopped {
				a.eof = true
			} else {
				kind := "protocol"
				if err == io.EOF || errors.Is(err, errTransport) {
					err, kind = errTransport, "transport"
				}
				if errors.Is(err, errVersion) {
					kind = "version"
				}
				a.failLocked(err, kind, kind != "transport")
			}
			a.mu.Unlock()
			return
		}
		if a.faults.Err() != nil {
			a.mu.Unlock()
			continue
		}
		if f.Run != a.run || f.Session != a.session {
			a.stale++
			a.mu.Unlock()
			continue
		}
		digest := sha256.Sum256(raw)
		if old, seen := a.digests[f.Seq]; seen {
			if old != digest {
				a.rejectLocked("conflicting duplicate sequence", "protocol")
			}
			a.mu.Unlock()
			continue
		}
		if f.Seq != a.received+1 {
			a.rejectLocked("sequence gap", "protocol")
			a.mu.Unlock()
			continue
		}
		a.received = f.Seq
		a.digests[f.Seq] = digest
		if f.Type == "ready" && a.startSent {
			a.mu.Unlock()
			select {
			case <-a.startDelivered:
			case <-a.faults.Done():
			}
			a.mu.Lock()
		}
		if f.Type == "stopped" && a.stopSent {
			a.mu.Unlock()
			select {
			case <-a.stopDelivered:
			case <-a.faults.Done():
			}
			a.mu.Lock()
		}
		// A legitimate immediate accepted can race Write's return. Wait for that
		// in-progress write, without holding the lock or assuming reservation is
		// evidence of delivery. An accepted for an unstarted write is invalid.
		if f.Type == "accepted" {
			w := a.invocations[str(f.Body["invocation"])]
			if w != nil && w.sending {
				a.mu.Unlock()
				select {
				case <-w.delivered:
				case <-a.faults.Done():
				}
				a.mu.Lock()
			}
		}
		if a.faults.Err() == nil && !a.receiveLocked(f) {
			a.failLocked(errProtocol, "protocol", true)
		}
		a.mu.Unlock()
	}
}

func (a *adapter) receiveLocked(f frame) bool {
	b := f.Body
	switch f.Type {
	case "fatal":
		// Never trust remote free-form text to be sanitized. Preserve kind and
		// vendor metadata, but use stable summaries for all published errors.
		a.failLocked(fmt.Errorf("external SUT %s failure", str(b["kind"])), str(b["kind"]), false)
		return true
	case "ready":
		if a.ready || !a.startSent || a.stopped {
			return false
		}
		if !equalNames(b["commands"], a.opts.Commands) || !equalNames(b["points"], a.opts.Points) || number(b["capacity"]) != a.opts.Capacity {
			return a.rejectLocked("ready registration mismatch", "startup")
		}
		a.ready = true
		close(a.readyDone)
		return true
	case "stopped":
		if !a.stopSent || a.stopped {
			return a.rejectLocked("unsolicited stopped before ready", "protocol")
		}
		for _, w := range a.workers {
			if w.terminal == nil {
				return false
			}
		}
		a.stopped = true
		return true
	case "accepted", "arrive", "terminal":
		if !a.ready || a.stopped {
			return false
		}
	default:
		return false
	}
	w := a.invocations[str(b["invocation"])]
	if w == nil {
		return a.rejectLocked("unknown invocation", "protocol")
	}
	if w.worker != b["worker"] {
		return false
	}
	if f.Type == "arrive" && !slices.Contains(a.opts.Points, str(b["point"])) {
		return false
	}
	if w.terminal != nil || w.retired {
		if f.Type == "terminal" && !reflect.DeepEqual(w.terminal, b) {
			return a.rejectLocked("conflicting retired terminal", "protocol")
		}
		return f.Type == "arrive" || (f.Type == "terminal" && reflect.DeepEqual(w.terminal, b))
	}
	switch f.Type {
	case "accepted":
		if w.accepted || !w.sending {
			return false
		}
		select {
		case <-w.delivered:
		default:
			return false
		}
		w.accepted = true
	case "arrive":
		if !w.accepted {
			return false
		}
		if w.parent.Err() != nil {
			a.cancelLocked(w, "context")
		}
		if w.reason != "" {
			return true
		}
		n, err := strconv.Atoi(str(b["arrival"]))
		if err != nil {
			return false
		}
		if w.outstanding || n != w.arrival+1 {
			return false
		}
		w.arrival, w.outstanding = n, true
		w.bridges++
		go a.bridge(w, b)
	case "terminal":
		if w.outstanding {
			return a.rejectLocked("terminal while arrival outstanding", "protocol")
		}
		if !w.accepted {
			return false
		}
		if e, ok := b["error"].(map[string]any); ok && e["kind"] == "cancelled" {
			if w.reason == "" || e["message"] != "cancelled by "+w.reason {
				return false
			}
		}
		w.terminal, w.terminalAt = b, a.now()
		if w.bridges == 0 {
			a.finishLocked(w)
		}
	}
	return true
}

func equalNames(v any, names []string) bool {
	a := v.([]any)
	if len(a) != len(names) {
		return false
	}
	for i, s := range names {
		if a[i] != s {
			return false
		}
	}
	return true
}

func (a *adapter) bridge(w *invocation, body map[string]any) {
	err := a.client.Arrive(w.ctx, w.worker, str(body["point"]))
	if a.beforeRelease != nil && err == nil {
		a.beforeRelease()
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if w.parent.Err() != nil {
		a.cancelLocked(w, "context")
	}
	if err != nil && (w.ctx.Err() == nil || (!errors.Is(err, context.Canceled) && !errors.Is(err, context.DeadlineExceeded))) {
		a.rejectLocked("runtime arrival failed", "protocol")
	} else if err == nil && w.reason == "" && a.faults.Err() == nil && !w.retired {
		a.enqueueLocked("release", body, nil)
		w.outstanding = false
	}
	w.bridges--
	if w.bridges == 0 && (w.terminal != nil || a.faults.Err() != nil) {
		a.finishLocked(w)
	}
}

func (a *adapter) finishLocked(w *invocation) {
	if w.retired {
		return
	}
	// receiveLocked stores only terminals validated before a session fault;
	// readLoop ignores subsequent frames. Bridge unwinding delays publication,
	// not the validity of that outcome. Preserve it without clearing the fault.
	if w.terminal != nil {
		err := terminalError(w)
		if w.terminal["transaction"] == "not_started" {
			w.results <- sut.InvocationOutcome{Unstarted: &sut.UnstartedResult{WorkerID: w.worker, Err: err}}
		} else {
			w.results <- sut.InvocationOutcome{Worker: &sut.WorkerResult{WorkerID: w.worker, Err: err, Duration: w.terminalAt.Sub(w.started)}}
		}
	}
	w.cancel()
	close(w.results)
	close(w.done)
	w.retired = true
	delete(a.workers, w.worker)
}

func terminalError(w *invocation) error {
	e, ok := w.terminal["error"].(map[string]any)
	if !ok {
		return nil
	}
	switch e["kind"] {
	case "mysql":
		code := number(e["mysql_code"])
		// The codec already enforces the wire range. Keep the narrowing
		// conversion locally bounded too, independent of that call path.
		if code < 1 || code > 65535 {
			return errProtocol
		}
		err := &mysql.MySQLError{Number: uint16(code), Message: "external SUT database command failed"}
		copy(err.SQLState[:], str(e["sql_state"]))
		return err
	case "cancelled":
		return fmt.Errorf("cancelled by %s: %w", w.reason, w.cause)
	default:
		return errors.New("external SUT application command failed")
	}
}
