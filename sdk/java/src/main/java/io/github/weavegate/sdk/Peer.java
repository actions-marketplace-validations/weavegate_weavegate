package io.github.weavegate.sdk;

import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.Executor;
import java.util.regex.Pattern;

import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * Java peer of external SUT wire v1. One instance serves exactly one session.
 *
 * <p>All state changes happen under this object's monitor. No pipe I/O, JDBC
 * call, application callback or blocking wait runs while it is held: frames are
 * appended to a non-blocking ordered output, and blocking work runs on worker,
 * startup, cleanup or cancellation threads.
 */
final class Peer {
    /** Fixed bound for a best-effort fatal write before a forced exit. */
    static final long FATAL_DELIVERY_BOUND_MILLIS = 100;

    private static final ThreadLocal<Invocation> CURRENT = new ThreadLocal<>();
    private static volatile Peer activeSession;
    private static final Pattern SQL_STATE = Pattern.compile("[0-9A-Z]{5}");

    enum Phase { AWAIT_START, STARTING, READY, STOPPING, STOPPED, FAILED }

    enum Proxy { NOT_ENTERED, INSIDE, EXITED, SKIPPED }

    enum Transaction { NONE, ACTIVE, COMMITTED, ROLLED_BACK, UNKNOWN }

    enum Lease { NOT_ACQUIRED, HELD, RETURNED, CLOSE_FAILED }

    enum GateState { WAITING, RELEASED, CANCELLED }

    private record DriverFailure(SQLException summary, long order) {
    }

    /** An installed sync-point wait. */
    final class Gate extends Parker {
        final String arrival;
        final String point;
        GateState state = GateState.WAITING;

        Gate(String arrival, String point) {
            super(activity);
            this.arrival = arrival;
            this.point = point;
        }
    }

    /** Invocation state; the tombstone is retained until the process exits. */
    final class Invocation {
        final String id;
        final String worker;
        final String command;
        int arrivals;
        Gate gate;
        final Map<String, String> released = new HashMap<>();
        final Map<String, String> cancelledArrivals = new HashMap<>();
        final List<Gate> gates = new ArrayList<>();
        String cancelReason;
        long cancelOrder;
        Seams.Timer cancelWatchdog;
        long cancelDeadline;
        Proxy proxy = Proxy.NOT_ENTERED;
        Thread thread;
        Transaction transaction = Transaction.NONE;
        Lease lease = Lease.NOT_ACQUIRED;
        Throwable source;
        long sourceOrder;
        final Map<SQLException, DriverFailure> driverFailures = new IdentityHashMap<>();
        final Set<Seams.Cancellable> statements = new LinkedHashSet<>();
        int jdbcCancelRequests;
        int dispatches;
        int terminals;
        ObjectNode terminal;
        boolean retired;

        Invocation(String id, String worker, String command) {
            this.id = id;
            this.worker = worker;
            this.command = command;
        }

        Peer peer() {
            return Peer.this;
        }
    }

    private final Seams.Host host;
    private final Seams.Clock clock;
    private final Seams.Exit exit;
    private final Seams.Output output;
    private final Seams.Threads threads;
    final Seams.Activity activity;

    Phase phase = Phase.AWAIT_START;
    Seams.Start start;
    private Map<String, Set<String>> commandPoints = Map.of();
    String run;
    String session;
    int received;
    private final Map<Integer, byte[]> digests = new HashMap<>();
    int sent;
    long staleFrames;
    private long order;
    final Map<String, Invocation> invocations = new LinkedHashMap<>();
    final Map<String, Invocation> workers = new HashMap<>();
    int live;
    private Executor executor;
    boolean admissionClosed;
    boolean startupDone;
    Thread probeThread;
    boolean startupCancelled;
    boolean cleanupStarted;
    boolean applicationClosed;
    boolean outputClosed;
    boolean exited;
    String fatalKind;
    boolean fatalSent;
    Seams.Timer startupWatchdog;
    long startupDeadline;
    Seams.Timer stopWatchdog;
    long stopDeadline;
    Seams.Timer fatalWatchdog;
    long fatalDeadline;
    int openLeases;

    Peer(Seams.Host host, Seams.Clock clock, Seams.Exit exit, Seams.Output output, Seams.Threads threads,
         Seams.Activity activity) {
        this.host = host;
        this.clock = clock;
        this.exit = exit;
        this.output = output;
        this.threads = threads;
        this.activity = activity;
    }

    static Invocation current() {
        return CURRENT.get();
    }

    static Peer activeSession() {
        return activeSession;
    }

    /** Marks this peer as the JVM's enabled session. Only the child bootstrap calls this. */
    void activate() {
        activeSession = this;
    }

    // ---------------------------------------------------------------- inbound

    /** Handles one complete payload in sender order; returns whether the reader should continue. */
    boolean receive(byte[] payload) {
        Wire.Frame frame;
        try {
            frame = Wire.decode(payload);
        } catch (Wire.WireException e) {
            synchronized (this) {
                if (run == null && e.run() != null) {
                    // A first start with an unsupported version still correlates its fatal reply.
                    run = e.run();
                    session = e.session();
                }
                fail(e.kind(), e.getMessage(), true);
                return false;
            }
        }
        synchronized (this) {
            if (phase == Phase.FAILED || phase == Phase.STOPPED || exited) {
                return false;
            }
            if (run != null && (!frame.run().equals(run) || !frame.session().equals(session))) {
                staleFrames++;
                return reading();
            }
            if (!Set.of("start", "invoke", "release", "cancel", "stop", "fatal").contains(frame.type())) {
                fail("protocol", "wrong message direction", true);
                return reading();
            }
            if (run == null) {
                if (!frame.type().equals("start") || frame.seq() != 1) {
                    fail("protocol", "first frame must be start", true);
                    return reading();
                }
                run = frame.run();
                session = frame.session();
            }
            byte[] digest = digest(frame.payload());
            if (frame.seq() <= received) {
                if (!Arrays.equals(digests.get(frame.seq()), digest)) {
                    fail("protocol", "conflicting duplicate sequence", true);
                }
                return reading();
            }
            if (frame.seq() != received + 1) {
                fail("protocol", "sequence gap", true);
                return reading();
            }
            received = frame.seq();
            digests.put(frame.seq(), digest);
            switch (frame.type()) {
                case "start" -> onStart(frame);
                case "invoke" -> onInvoke(frame);
                case "release" -> onRelease(frame);
                case "cancel" -> onCancel(frame);
                case "stop" -> onStop(frame);
                case "fatal" -> fail(frame.text("kind"), "engine fatal", false);
                default -> fail("protocol", "wrong message direction", true);
            }
            return reading();
        }
    }

    private boolean reading() {
        return phase != Phase.FAILED && phase != Phase.STOPPED && !exited;
    }

    /** Framing, EOF or read failure reported by the control reader. */
    synchronized void readFailed(String kind, String message) {
        if (phase == Phase.STOPPED || exited) {
            return;
        }
        fail(kind, message, true);
    }

    /** Output stream failure reported by the writer; nothing more can be delivered. */
    synchronized void writeFailed() {
        if (phase == Phase.STOPPED || exited) {
            return;
        }
        outputClosed = true;
        fail("transport", "control output failed", false);
    }

    private void onStart(Wire.Frame frame) {
        if (start != null) {
            fail("protocol", "duplicate start", true);
            return;
        }
        ObjectNode body = frame.body();
        Map<String, String> params = new LinkedHashMap<>();
        for (Map.Entry<String, JsonNode> entry : body.get("params").properties()) {
            params.put(entry.getKey(), entry.getValue().stringValue());
        }
        JsonNode db = body.get("database");
        start = new Seams.Start(frame.text("variant"), Map.copyOf(params), strings(body.get("commands")),
                strings(body.get("points")), frame.integer("capacity"),
                new Seams.Database(db.get("host").stringValue(), db.get("port").intValue(),
                        db.get("name").stringValue(), db.get("username").stringValue(),
                        db.get("password").stringValue()),
                frame.integer("startup_ms"), frame.integer("cancel_ms"));
        phase = Phase.STARTING;
        startupDeadline = clock.nowMillis() + start.startupMillis();
        startupWatchdog = clock.schedule(start.startupMillis(), this::startupExpired);
        Seams.Start config = start;
        threads.start("weavegate-startup", () -> startup(config));
    }

    private void startup(Seams.Start config) {
        Throwable failure = null;
        Map<String, Set<String>> validated = Map.of();
        try {
            host.initialize(config);
            synchronized (this) {
                if (startupCancelled || phase == Phase.FAILED) {
                    throw new IllegalStateException("startup cancelled");
                }
            }
            validated = host.validateRegistration(config.commands(), config.points());
            synchronized (this) {
                probeThread = Thread.currentThread();
            }
            try {
                host.probeDatabase();
            } finally {
                synchronized (this) {
                    probeThread = null;
                }
            }
        } catch (Throwable t) {
            failure = t;
        }
        synchronized (this) {
            startupDone = true;
            if (exited) {
                return;
            }
            if (phase == Phase.STARTING && (failure != null || openLeases != 0)) {
                fail("startup", "application startup failed", true);
                return;
            }
            if (phase == Phase.STARTING) {
                commandPoints = validated;
                phase = Phase.READY;
                startupWatchdog.cancel();
                startupWatchdog = null;
                executor = threads.workers(config.capacity());
                ObjectNode body = Wire.object();
                body.set("commands", array(config.commands()));
                body.set("points", array(config.points()));
                body.put("capacity", config.capacity());
                send("ready", body);
                return;
            }
            finishCleanup();
        }
    }

    private void onInvoke(Wire.Frame frame) {
        if (phase != Phase.READY) {
            fail("protocol", "invoke outside ready state", true);
            return;
        }
        String id = frame.text("invocation");
        String worker = frame.text("worker");
        String command = frame.text("command");
        if (invocations.containsKey(id)) {
            fail("protocol", "duplicate invocation", true);
            return;
        }
        if (!start.commands().contains(command)) {
            fail("protocol", "unknown command", true);
            return;
        }
        if (workers.containsKey(worker)) {
            fail("protocol", "worker already active", true);
            return;
        }
        if (live >= start.capacity()) {
            fail("protocol", "capacity exceeded", true);
            return;
        }
        Invocation invocation = new Invocation(id, worker, command);
        invocations.put(id, invocation);
        workers.put(worker, invocation);
        live++;
        if (!send("accepted", identity(invocation))) {
            // Nothing was dispatched, so cancellation cannot move NOT_ENTERED
            // forward. Retire directly after send() latches the fatal failure.
            invocation.proxy = Proxy.SKIPPED;
            retire(invocation);
            finishCleanup();
            return;
        }
        invocation.dispatches++;
        executor.execute(new InvocationTask(invocation));
    }

    /** Worker task wrapper; harnesses may identify the invocation before running it. */
    final class InvocationTask implements Runnable {
        final Invocation invocation;

        InvocationTask(Invocation invocation) {
            this.invocation = invocation;
        }

        @Override
        public void run() {
            execute(invocation);
        }
    }

    private void execute(Invocation invocation) {
        synchronized (this) {
            if (exited || invocation.cancelReason != null || phase == Phase.FAILED) {
                invocation.proxy = Proxy.SKIPPED;
                progress(invocation);
                return;
            }
            invocation.proxy = Proxy.INSIDE;
            invocation.thread = Thread.currentThread();
        }
        Throwable thrown = null;
        CURRENT.set(invocation);
        try {
            host.execute(new CommandContext(invocation.id, invocation.worker, invocation.command, start.variant(),
                    start.params()));
        } catch (Throwable t) {
            thrown = t;
        } finally {
            CURRENT.remove();
        }
        synchronized (this) {
            if (thrown != null) {
                recordSourceLocked(invocation, thrown);
            }
            invocation.driverFailures.clear();
            invocation.proxy = Proxy.EXITED;
            invocation.thread = null;
            progress(invocation);
        }
    }

    private void onRelease(Wire.Frame frame) {
        Invocation invocation = bound(frame);
        if (invocation == null || invocation.retired) {
            return;
        }
        String arrival = frame.text("arrival");
        String point = frame.text("point");
        int number = Integer.parseInt(arrival);
        if (invocation.arrivals == 0) {
            fail("protocol", "release before arrival", true);
            return;
        }
        if (number > invocation.arrivals) {
            fail("protocol", "future release", true);
            return;
        }
        Gate gate = invocation.gate;
        if (gate != null && gate.arrival.equals(arrival)) {
            if (!gate.point.equals(point)) {
                fail("protocol", "release identity mismatch", true);
                return;
            }
            gate.state = GateState.RELEASED;
            invocation.gate = null;
            invocation.released.put(arrival, point);
            gate.signal();
            return;
        }
        String recorded = invocation.released.containsKey(arrival) ? invocation.released.get(arrival)
                : invocation.cancelledArrivals.get(arrival);
        if (recorded == null || !recorded.equals(point)) {
            fail("protocol", "release identity mismatch", true);
        }
        // A duplicate of a recorded release, or a release racing cancellation, has no effect.
    }

    private void onCancel(Wire.Frame frame) {
        Invocation invocation = bound(frame);
        if (invocation == null || invocation.retired) {
            return;
        }
        cancel(invocation, frame.text("reason"));
    }

    private void onStop(Wire.Frame frame) {
        if (phase == Phase.STOPPING) {
            fail("protocol", "duplicate stop", true);
            return;
        }
        boolean starting = phase == Phase.STARTING;
        phase = Phase.STOPPING;
        admissionClosed = true;
        int budget = frame.integer("budget_ms");
        stopDeadline = clock.nowMillis() + budget;
        stopWatchdog = clock.schedule(budget, this::stopExpired);
        if (starting) {
            startupCancelled = true;
            host.cancelStartup();
        }
        for (Invocation invocation : List.copyOf(invocations.values())) {
            if (!invocation.retired) {
                cancel(invocation, "stop");
            }
        }
        finishCleanup();
    }

    private Invocation bound(Wire.Frame frame) {
        Invocation invocation = invocations.get(frame.text("invocation"));
        if (invocation == null) {
            fail("protocol", "unknown invocation", true);
            return null;
        }
        if (!invocation.worker.equals(frame.text("worker"))) {
            fail("protocol", "invocation binding changed", true);
            return null;
        }
        return invocation;
    }

    // ----------------------------------------------------------- cancellation

    /** Latches the first cancellation reason irreversibly and starts bounded cleanup. */
    private void cancel(Invocation invocation, String reason) {
        if (invocation.retired || invocation.cancelReason != null) {
            return;
        }
        invocation.cancelReason = reason;
        invocation.cancelOrder = ++order;
        Gate gate = invocation.gate;
        if (gate != null) {
            gate.state = GateState.CANCELLED;
            invocation.gate = null;
            invocation.cancelledArrivals.put(gate.arrival, gate.point);
            gate.signal();
        }
        if (!reason.equals("fatal")) {
            invocation.cancelDeadline = clock.nowMillis() + start.cancelMillis();
            invocation.cancelWatchdog = clock.schedule(start.cancelMillis(), () -> cancelExpired(invocation));
        }
        invocation.jdbcCancelRequests++;
        List<Seams.Cancellable> statements = List.copyOf(invocation.statements);
        if (!statements.isEmpty()) {
            threads.start("weavegate-jdbc-cancel", () -> {
                for (Seams.Cancellable statement : statements) {
                    try {
                        statement.cancel();
                    } catch (Exception ignored) {
                        // A cancel request is not proof of completion; transaction milestones decide.
                    }
                }
            });
        }
        progress(invocation);
    }

    void arrive(Invocation invocation, String point) {
        Gate gate;
        synchronized (this) {
            if (exited || phase == Phase.FAILED || invocation.cancelReason != null) {
                throw cancelled(invocation);
            }
            if (Thread.currentThread() != invocation.thread || invocation.proxy != Proxy.INSIDE) {
                fail("protocol", "sync point outside invocation thread", true);
                throw cancelled(invocation);
            }
            if (!Wire.name(point) || !start.points().contains(point)
                    || !commandPoints.getOrDefault(invocation.command, Set.of()).contains(point)) {
                fail("protocol", "unknown sync point", true);
                throw cancelled(invocation);
            }
            if (invocation.arrivals >= Wire.MAX_ARRIVAL) {
                fail("protocol", "arrival sequence exhausted", true);
                throw cancelled(invocation);
            }
            invocation.arrivals++;
            gate = new Gate(Integer.toString(invocation.arrivals), point);
            invocation.gate = gate;
            invocation.gates.add(gate);
            ObjectNode body = identity(invocation);
            body.put("arrival", gate.arrival);
            body.put("point", point);
            send("arrive", body);
        }
        try {
            gate.await();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            synchronized (this) {
                fail("protocol", "sync point interrupted", true);
            }
            throw cancelled(invocation);
        }
        synchronized (this) {
            if (gate.state != GateState.RELEASED) {
                throw cancelled(invocation);
            }
        }
    }

    synchronized void outsideInvocation() {
        fail("protocol", "sync point outside invocation", true);
        throw new WeavegateCancelledException("session failed");
    }

    private WeavegateCancelledException cancelled(Invocation invocation) {
        String reason = invocation.cancelReason;
        return new WeavegateCancelledException(reason == null || reason.equals("fatal") ? "session failed"
                : "cancelled by " + reason);
    }

    // ------------------------------------------------------------- milestones

    synchronized void transactionBegun(Invocation invocation) {
        if (invocation.transaction != Transaction.NONE) {
            fail("transaction", "unsupported additional transaction", true);
            return;
        }
        invocation.transaction = Transaction.ACTIVE;
    }

    synchronized void transactionCompleted(Invocation invocation, Transaction outcome) {
        if (outcome == Transaction.NONE || outcome == Transaction.ACTIVE) {
            throw new IllegalArgumentException("completion requires an outcome");
        }
        if (invocation.transaction != Transaction.ACTIVE) {
            fail("transaction", "transaction completion without begin", true);
            return;
        }
        invocation.transaction = outcome;
        progress(invocation);
    }

    synchronized boolean leaseAcquired(Invocation invocation) {
        if (invocation == null) {
            // Only the SDK's readiness probe may lease without an invocation.
            // It may finish after a pre-ready Stop changes the phase to STOPPING.
            if (Thread.currentThread() != probeThread || startupDone
                    || (phase != Phase.STARTING && phase != Phase.STOPPING)) {
                fail("protocol", "database lease outside invocation", true);
                return false;
            }
            openLeases++;
            return true;
        }
        if (invocation.lease != Lease.NOT_ACQUIRED) {
            fail("cleanup", "unsupported additional connection lease", true);
            return false;
        }
        openLeases++;
        invocation.lease = Lease.HELD;
        return true;
    }

    synchronized void leaseReturned(Invocation invocation) {
        openLeases--;
        if (invocation != null && invocation.lease == Lease.HELD) {
            invocation.lease = Lease.RETURNED;
            progress(invocation);
        }
    }

    synchronized void leaseCloseFailed(Invocation invocation) {
        if (invocation == null) {
            fail("cleanup", "lease return failed", true);
            return;
        }
        if (invocation.lease == Lease.HELD) {
            invocation.lease = Lease.CLOSE_FAILED;
            progress(invocation);
        }
    }

    synchronized void recordSource(Invocation invocation, Throwable source) {
        recordSourceLocked(invocation, source);
    }

    private void recordSourceLocked(Invocation invocation, Throwable source) {
        if (invocation.source == null) {
            DriverFailure driver = escapingDriverFailure(invocation, source);
            invocation.source = driver == null ? source : driver.summary();
            invocation.sourceOrder = driver == null ? ++order : driver.order();
        }
    }

    synchronized void driverFailure(Invocation invocation, SQLException failure, SQLException summary) {
        invocation.driverFailures.computeIfAbsent(failure, ignored -> new DriverFailure(summary, ++order));
        // InnoDB rolls back the whole transaction on error 1213. A caught
        // exception cannot restore the original commit/rollback evidence.
        if (failure.getErrorCode() == 1213 && invocation.transaction == Transaction.ACTIVE) {
            invocation.transaction = Transaction.UNKNOWN;
            fail("transaction", "transaction outcome unknown", true);
        }
    }

    /** Requires retained JDBC handles to remain on their owning live invocation thread. */
    synchronized void jdbcEntry(Invocation invocation, boolean sdkCleanup) {
        if (CURRENT.get() != invocation || Thread.currentThread() != invocation.thread
                || invocation.proxy != Proxy.INSIDE) {
            fail("protocol", "JDBC operation outside invocation thread", true);
            throw cancelled(invocation);
        }
        if (invocation.transaction != Transaction.ACTIVE && !sdkCleanup) {
            fail("transaction", "JDBC operation outside active transaction", true);
            throw cancelled(invocation);
        }
    }

    private static DriverFailure escapingDriverFailure(Invocation invocation, Throwable source) {
        Set<Throwable> seen = java.util.Collections.newSetFromMap(new IdentityHashMap<>());
        for (Throwable candidate = source; candidate != null && seen.add(candidate); candidate = candidate.getCause()) {
            DriverFailure observed = invocation.driverFailures.get(candidate);
            if (observed != null) {
                return observed;
            }
        }
        return null;
    }

    /** Registers an executing statement; returns false if cancellation already won. */
    synchronized boolean statementStarted(Invocation invocation, Seams.Cancellable statement) {
        invocation.statements.add(statement);
        return invocation.cancelReason == null;
    }

    synchronized void statementFinished(Invocation invocation, Seams.Cancellable statement) {
        invocation.statements.remove(statement);
    }

    synchronized void unsupported(String kind, String message) {
        fail(kind, message, true);
    }

    /** Publishes a terminal only after proxy exit, known outcome and lease return. */
    private void progress(Invocation invocation) {
        if (invocation.retired || exited) {
            return;
        }
        if (invocation.transaction == Transaction.UNKNOWN) {
            fail("transaction", "transaction outcome unknown", true);
            return;
        }
        if (invocation.lease == Lease.CLOSE_FAILED) {
            fail("cleanup", "lease return failed", true);
            return;
        }
        boolean outside = invocation.proxy == Proxy.EXITED || invocation.proxy == Proxy.SKIPPED;
        String transaction;
        String connection;
        if (outside && invocation.transaction == Transaction.NONE && invocation.lease == Lease.NOT_ACQUIRED) {
            transaction = "not_started";
            connection = "not_acquired";
        } else if (invocation.proxy == Proxy.EXITED && invocation.lease == Lease.RETURNED
                && invocation.transaction != Transaction.ACTIVE) {
            transaction = switch (invocation.transaction) {
                case COMMITTED -> "committed";
                case ROLLED_BACK -> "rolled_back";
                default -> "not_started";
            };
            connection = "returned";
        } else {
            return;
        }
        if (invocation.gate != null) {
            fail("protocol", "terminal with outstanding arrival", true);
            return;
        }
        if (phase == Phase.FAILED) {
            // Known cleanup remains local after fatal; no new worker terminal is emitted.
            retire(invocation);
            finishCleanup();
            return;
        }
        ObjectNode body = identity(invocation);
        body.put("transaction", transaction);
        body.put("connection", connection);
        ObjectNode error = error(invocation, transaction);
        if (error == null) {
            body.putNull("error");
        } else {
            body.set("error", error);
        }
        invocation.terminal = body;
        invocation.terminals++;
        send("terminal", body);
        retire(invocation);
        finishCleanup();
    }

    private void retire(Invocation invocation) {
        // send() may synchronously fail the session and retire this invocation
        // through cancellation before its original progress() call resumes.
        if (invocation.retired) {
            return;
        }
        invocation.retired = true;
        if (invocation.cancelWatchdog != null) {
            invocation.cancelWatchdog.cancel();
        }
        if (workers.get(invocation.worker) == invocation) {
            workers.remove(invocation.worker);
        }
        live--;
    }

    private ObjectNode error(Invocation invocation, String transaction) {
        boolean cancelled = invocation.cancelReason != null && !invocation.cancelReason.equals("fatal");
        Throwable source = invocation.source;
        boolean application = source != null && !(source instanceof WeavegateCancelledException);
        if (application && (!cancelled || invocation.sourceOrder < invocation.cancelOrder)) {
            return classify(source);
        }
        if (transaction.equals("committed") && source == null) {
            return null;
        }
        if (cancelled) {
            return errorBody("cancelled", "cancelled by " + invocation.cancelReason, 0, "");
        }
        if (application) {
            return classify(source);
        }
        return errorBody("application", transaction.equals("rolled_back") ? "transaction rolled back"
                : transaction.equals("committed") ? "command failed after commit" : "transaction not started", 0, "");
    }

    static ObjectNode classify(Throwable source) {
        Set<Throwable> seen = java.util.Collections.newSetFromMap(new IdentityHashMap<>());
        for (Throwable t = source; t != null && seen.add(t); t = t.getCause()) {
            if (t instanceof SQLException sql && sql.getErrorCode() > 0 && sql.getErrorCode() <= 65535
                    && sql.getSQLState() != null && SQL_STATE.matcher(sql.getSQLState()).matches()) {
                return errorBody("mysql", message(sql), sql.getErrorCode(), sql.getSQLState());
            }
        }
        return errorBody("application", message(source), 0, "");
    }

    private static String message(Throwable t) {
        String message = t.getMessage();
        return Wire.sanitize(message == null || message.isBlank() ? t.getClass().getSimpleName() : message);
    }

    private static ObjectNode errorBody(String kind, String message, int code, String state) {
        ObjectNode error = Wire.object();
        error.put("kind", kind);
        error.put("message", Wire.sanitize(message));
        error.put("mysql_code", code);
        error.put("sql_state", state);
        return error;
    }

    // ------------------------------------------------------ cleanup and exit

    /** Starts pool/application closure once admission is closed and no invocation remains. */
    private void finishCleanup() {
        if ((phase != Phase.STOPPING && phase != Phase.FAILED) || cleanupStarted || live > 0 || exited) {
            return;
        }
        if (start != null && !startupDone) {
            return;
        }
        cleanupStarted = true;
        boolean failed = phase == Phase.FAILED;
        threads.start("weavegate-cleanup", () -> {
            Throwable failure = null;
            try {
                host.close();
            } catch (Throwable t) {
                failure = t;
            }
            synchronized (this) {
                if (exited) {
                    return;
                }
                if (failure != null) {
                    forceExit("shutdown", "application shutdown failed");
                    return;
                }
                applicationClosed = true;
                if (openLeases != 0 && phase != Phase.FAILED) {
                    fail("cleanup", "application retained a database lease", true);
                }
                if (!failed && phase == Phase.STOPPING) {
                    send("stopped", Wire.object());
                    output.closeAfterDrain(() -> closedThenExit(0));
                } else {
                    output.closeAfterDrain(() -> closedThenExit(1));
                }
            }
        });
    }

    private void closedThenExit(int status) {
        synchronized (this) {
            outputClosed = true;
            if (exited) {
                return;
            }
            if (phase == Phase.FAILED) {
                status = 1;
            }
            if (status == 0) {
                phase = Phase.STOPPED;
            }
            exited = true;
        }
        exit.halt(status);
    }

    /**
     * Latches a session failure. After fatal no worker terminal or stopped is
     * emitted; active gates wake exceptionally and cleanup stays bounded.
     */
    private void fail(String kind, String message, boolean send) {
        if (phase == Phase.FAILED || phase == Phase.STOPPED || exited) {
            return;
        }
        boolean hadStart = start != null;
        phase = Phase.FAILED;
        admissionClosed = true;
        fatalKind = kind;
        if (send) {
            fatalSent = sendFatal(kind, message);
        }
        if (!hadStart) {
            forceExit(null, null);
            return;
        }
        if (!startupDone) {
            startupCancelled = true;
            host.cancelStartup();
        }
        // One absolute post-fatal bound: the earliest existing cleanup deadline, or cancel_ms
        // from detection when none exists. Retiring an invocation cannot remove this bound.
        long now = clock.nowMillis();
        long deadline = now + start.cancelMillis();
        if (stopWatchdog != null) {
            deadline = Math.min(deadline, stopDeadline);
        }
        for (Invocation invocation : invocations.values()) {
            if (!invocation.retired && invocation.cancelWatchdog != null) {
                deadline = Math.min(deadline, invocation.cancelDeadline);
            }
        }
        fatalDeadline = deadline;
        fatalWatchdog = clock.schedule(Math.max(0, deadline - now), () -> {
            synchronized (this) {
                forceExit(null, null);
            }
        });
        for (Invocation invocation : List.copyOf(invocations.values())) {
            if (!invocation.retired) {
                cancel(invocation, "fatal");
            }
        }
        finishCleanup();
    }

    private boolean sendFatal(String kind, String message) {
        if (sent >= Wire.MAX_SEQ || outputClosed) {
            return false;
        }
        ObjectNode body = Wire.object();
        body.put("kind", kind);
        body.put("message", Wire.sanitize(message));
        return send("fatal", body);
    }

    private void startupExpired() {
        synchronized (this) {
            if (startupWatchdog != null && !startupDone) {
                forceExit("startup", "startup deadline exceeded");
            }
        }
    }

    private void stopExpired() {
        synchronized (this) {
            forceExit("shutdown", "stop deadline exceeded");
        }
    }

    private void cancelExpired(Invocation invocation) {
        synchronized (this) {
            if (!invocation.retired) {
                forceExit("cleanup", "cancellation cleanup deadline exceeded");
            }
        }
    }

    /**
     * Watchdog expiry: best-effort fatal (unless already failed or stopped) and a
     * nonzero exit that never waits for cleanup, rollback or shutdown hooks.
     */
    private void forceExit(String kind, String message) {
        if (exited) {
            return;
        }
        if (kind != null && phase != Phase.FAILED && phase != Phase.STOPPED) {
            phase = Phase.FAILED;
            admissionClosed = true;
            fatalKind = kind;
            fatalSent = sendFatal(kind, message);
        }
        exited = true;
        output.drainWithin(FATAL_DELIVERY_BOUND_MILLIS, () -> exit.halt(1));
    }

    // ---------------------------------------------------------------- helpers

    private boolean send(String type, ObjectNode body) {
        if (exited || outputClosed) {
            return false;
        }
        if (sent >= Wire.MAX_SEQ) {
            if (!type.equals("fatal")) {
                fail("protocol", "sequence exhausted", false);
            }
            return false;
        }
        sent++;
        // Before any identity is bound, a fatal reply uses the all-zero correlation value.
        String unbound = "0".repeat(32);
        if (!output.enqueue(Wire.encode(type, run == null ? unbound : run, session == null ? unbound : session, sent, body))) {
            outputClosed = true;
            if (!type.equals("fatal")) {
                fail("transport", "control output overflow", false);
            }
            return false;
        }
        return true;
    }

    private static ObjectNode identity(Invocation invocation) {
        ObjectNode body = Wire.object();
        body.put("invocation", invocation.id);
        body.put("worker", invocation.worker);
        return body;
    }

    private static ArrayNode array(List<String> values) {
        ArrayNode array = Wire.object().arrayNode();
        values.forEach(array::add);
        return array;
    }

    private static List<String> strings(JsonNode node) {
        List<String> values = new ArrayList<>();
        node.values().forEach(value -> values.add(value.stringValue()));
        return List.copyOf(values);
    }

    private static byte[] digest(byte[] payload) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(payload);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 unavailable", e);
        }
    }
}
