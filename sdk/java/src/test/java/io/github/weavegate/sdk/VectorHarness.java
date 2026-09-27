package io.github.weavegate.sdk;

import java.sql.SQLException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.Set;

import tools.jackson.databind.JsonNode;

/**
 * Executes one shared vector case against a fresh Java peer and a scripted
 * engine. Opposite-peer local steps are the script, not assertions. Every Java
 * event and assertion has a closed handler; anything unknown fails before
 * injection. Observers read state and never inject input or completion.
 */
final class VectorHarness {
    static final String HANDLER = "sdk/java/src/test/java/io/github/weavegate/sdk/VectorHarness.java:VectorHarness.";
    private static final Set<String> PHASES = Set.of("command_body", "jdbc_operation", "after_commit_callback");
    private static final Set<String> EXCEPTIONS = Set.of("java.lang.IllegalStateException", "java.lang.RuntimeException",
            "java.sql.SQLException");
    private static final Set<String> ASSERTIONS = Set.of(
            "initialize_application", "validate_registration", "probe_database", "send_ready", "send_arrive",
            "send_terminal", "send_stopped", "reserve_invocation", "dispatch_once", "install_gate",
            "wake_exact_gate", "no_terminal", "no_stopped", "no_ready", "no_reply", "cancel_latched",
            "wake_gate_exceptionally", "arm_cleanup_watchdog", "request_jdbc_cancel", "close_admission",
            "retain_earlier_cleanup_deadline", "await_active_cleanup", "no_child_exit", "retire_invocation",
            "no_resume", "force_nonzero_exit", "exit_nonzero", "record_source_exception", "cleanup_still_blocked",
            "no_cleanup_success", "fatal_protocol", "fatal_cleanup", "fatal_version", "fatal_startup",
            "fatal_shutdown", "fatal_transaction", "rollback_barrier_armed", "close_pool_and_application",
            "consume_retired_invocation", "begin_bounded_cleanup", "no_arrive_for_w2", "database_lock_released",
            "ignore_cancelled_gate", "prevent_command_start", "committed_terminal_unchanged", "no_second_terminal",
            "no_rollback", "no_fatal", "no_command_start", "cancel_startup", "close_control_stream",
            "initialization_barrier_armed", "arm_startup_watchdog", "initialization_blocked",
            "initialization_still_blocked", "application_shutdown_barrier_armed", "arm_stop_watchdog",
            "application_cleanup_blocked", "ignore_duplicate", "no_redispatch",
            "outbound_sequence_at_limit", "arrival_sequence_at_limit", "fatal_without_wire");

    final String row;
    final Fakes.Activity activity = new Fakes.Activity();
    final Fakes.Clock clock = new Fakes.Clock();
    final Fakes.Exit exit = new Fakes.Exit();
    final Fakes.Output output = new Fakes.Output();
    final Fakes.Threads threads = new Fakes.Threads(activity);
    final Fakes.Host host = new Fakes.Host(activity);
    final Peer peer = new Peer(host, clock, exit, output, threads, activity);
    private final Map<String, Long> anchors = new HashMap<>();
    private final Map<String, Throwable> injected = new HashMap<>();
    private String firstInvocation;
    private int consumed;
    private boolean record = true;

    VectorHarness(String row) {
        this.row = row;
        host.peer = peer;
    }

    /** Test seam for dispatch-closure tests: validation failures must not emit evidence. */
    VectorHarness quiet() {
        record = false;
        return this;
    }

    void run(List<JsonNode> steps) {
        try {
            for (int i = 0; i < steps.size(); i++) {
                step(i, steps.get(i));
            }
            if (consumed != output.frames.size()) {
                throw new AssertionError("unexpected extra peer output");
            }
        } finally {
            threads.interruptAll();
        }
    }

    void step(int index, JsonNode step) {
        String base = "step/" + index;
        String peerName = step.get("peer").stringValue();
        String action = step.get("action").stringValue();
        if (peerName.equals("java")) {
            for (JsonNode assertion : step.get("expect")) {
                require(ASSERTIONS.contains(assertion.stringValue()), "unhandled assertion " + assertion.stringValue());
            }
        }
        if (peerName.equals("go")) {
            if (action.equals("receive") && step.get("delivery").stringValue().equals("exchange")) {
                JsonNode want = step.get("frame");
                if (consumed >= output.frames.size()) {
                    throw new AssertionError(row + " " + base + ": missing peer output " + want.get("type"));
                }
                JsonNode got = output.frames.get(consumed++);
                if (!got.equals(want)) {
                    throw new AssertionError(row + " " + base + ": output mismatch\nwant " + want + "\ngot  " + got);
                }
                report(base + "/output/" + want.get("type").stringValue(), "step");
            }
            return;
        }
        if (!peerName.equals("java")) {
            throw new AssertionError("unknown vector peer");
        }
        Context context = new Context(step, output.frames.size(), peer.received, progressCount());
        switch (action) {
            case "receive" -> {
                JsonNode frame = step.get("frame");
                context.frame = frame;
                context.invocation = frame.get("body").has("invocation") ? frame.get("body").get("invocation").stringValue() : null;
                String type = frame.get("type").stringValue();
                if (type.equals("invoke") && firstInvocation == null) {
                    firstInvocation = context.invocation;
                }
                anchors.putIfAbsent(switch (type) {
                    case "start" -> "startup";
                    case "cancel" -> "cancel";
                    case "stop" -> "stop";
                    case "fatal" -> "fatal";
                    default -> "other";
                }, clock.nowMillis());
                peer.receive(Vectors.JSON.writeValueAsBytes(frame));
                activity.awaitIdle();
                if (type.equals("stop") && host.closeStarted && !host.holdShutdown && !host.shutdown.signalled()) {
                    // Closure requested synchronously by stop has no held resource; a deferred or
                    // held closure waits for an explicit application_cleanup_complete milestone.
                    host.shutdown.signal();
                    activity.awaitIdle();
                }
                report(base + "/receive/" + type, "step");
            }
            case "local" -> {
                local(step.get("event").stringValue(), step.get("args"), context);
                report(base + "/local/" + step.get("event").stringValue(), "step");
            }
            default -> throw new AssertionError("unknown vector action");
        }
        JsonNode expect = step.get("expect");
        for (int i = 0; i < expect.size(); i++) {
            String label = expect.get(i).stringValue();
            observe(label, context);
            report(base + "/expect/" + i + "/" + label, "observe");
        }
    }

    private void report(String check, String method) {
        if (record) {
            EvidenceListener.check(row, check, HANDLER + method);
        }
    }

    private static final class Context {
        final JsonNode step;
        final int outputsBefore;
        final int receivedBefore;
        JsonNode frame;
        JsonNode args;
        String invocation;
        String arrival;
        String point;
        String worker;
        final long progressBefore;

        Context(JsonNode step, int outputsBefore, int receivedBefore, long progressBefore) {
            this.step = step;
            this.outputsBefore = outputsBefore;
            this.receivedBefore = receivedBefore;
            this.progressBefore = progressBefore;
        }
    }

    private Peer.Invocation invocationSnapshot(String id) {
        synchronized (peer) {
            return id == null ? null : peer.invocations.get(id);
        }
    }

    // ------------------------------------------------------------------ events

    /** Injects one local event outside a vector step, for dispatch-closure tests. */
    void local(String event, JsonNode args) {
        local(event, args, new Context(null, output.frames.size(), peer.received, progressCount()));
    }

    private void local(String event, JsonNode args, Context context) {
        context.args = args;
        switch (event) {
            case "readiness_complete" -> {
                fields(args, "probe_lease_returned");
                require(args.get("probe_lease_returned").isBoolean() && args.get("probe_lease_returned").booleanValue(),
                        "probe lease must be returned");
                require(host.probeStarted && !host.probe.signalled(), "no pending readiness probe");
                host.completeProbe();
                activity.awaitIdle();
            }
            case "worker_arrives" -> {
                fields(args, "identity");
                JsonNode identity = args.get("identity");
                fields(identity, "invocation", "worker", "arrival", "point");
                context.invocation = text(identity, "invocation");
                context.worker = text(identity, "worker");
                context.arrival = text(identity, "arrival");
                context.point = text(identity, "point");
                Peer.Invocation invocation = live(context.invocation, context.worker);
                synchronized (peer) {
                    require(Integer.toString(invocation.arrivals + 1).equals(context.arrival), "unexpected arrival number");
                }
                runTask(invocation.id);
                host.script(invocation.id).mailbox.put(new Fakes.Arrive(context.point));
                activity.awaitIdle();
            }
            case "exhaust_outbound_sequence" -> {
                fields(args, "limit");
                require(args.get("limit").intValue() == Wire.MAX_SEQ, "outbound sequence limit differs");
                synchronized (peer) {
                    require(peer.phase == Peer.Phase.READY && peer.sent == 1, "sequence setup requires ready peer");
                    peer.sent = Wire.MAX_SEQ;
                }
            }
            case "exhaust_arrivals" -> {
                fields(args, "invocation", "limit");
                context.invocation = text(args, "invocation");
                require(args.get("limit").intValue() == Wire.MAX_ARRIVAL, "arrival limit differs");
                Peer.Invocation invocation = live(context.invocation, null);
                synchronized (peer) {
                    require(invocation.arrivals == 0 && invocation.gate == null, "arrival setup requires unused gate");
                    invocation.arrivals = Wire.MAX_ARRIVAL;
                }
            }
            case "completion" -> completion(args, context);
            case "command_exception" -> {
                Throwable exception = exception(args);
                context.invocation = text(args, "invocation");
                Peer.Invocation invocation = live(context.invocation, null);
                injected.put(invocation.id, exception);
                runTask(invocation.id);
                host.script(invocation.id).mailbox.put(new Fakes.Fail(exception));
                activity.awaitIdle();
            }
            case "jdbc_blocked" -> {
                fields(args, "worker");
                context.worker = text(args, "worker");
                Peer.Invocation invocation;
                synchronized (peer) {
                    invocation = peer.workers.get(context.worker);
                }
                require(invocation != null, "unknown worker");
                context.invocation = invocation.id;
                runTask(invocation.id);
                host.script(invocation.id).mailbox.put(new Fakes.BlockJdbc());
                activity.awaitIdle();
            }
            case "hold_cleanup" -> {
                fields(args, "phase");
                switch (text(args, "phase")) {
                    case "rollback" -> host.holdRollback = true;
                    case "initialization" -> host.holdInitialization = true;
                    case "application_shutdown" -> host.holdShutdown = true;
                    default -> throw new AssertionError("unknown cleanup phase");
                }
            }
            case "advance_cancel_cleanup_clock" -> advance("cancel", args);
            case "advance_fatal_cleanup_clock" -> advance("fatal", args);
            case "advance_startup_clock" -> advance("startup", args);
            case "advance_stop_clock" -> advance("stop", args);
            case "application_cleanup_complete" -> {
                fields(args, "active_invocations", "open_leases", "pool_closed", "application_closed");
                synchronized (peer) {
                    require(args.get("active_invocations").isInt() && args.get("active_invocations").intValue() == peer.live,
                            "active invocation milestone differs from peer state");
                    require(args.get("open_leases").isInt() && args.get("open_leases").intValue() == peer.openLeases,
                            "lease milestone differs from tracked leases");
                }
                require(args.get("pool_closed").isBoolean() && args.get("pool_closed").booleanValue()
                        && args.get("application_closed").isBoolean() && args.get("application_closed").booleanValue(),
                        "cleanup milestone must close pool and application");
                require(host.closeStarted && !host.holdShutdown, "no releasable application cleanup");
                host.shutdown.signal();
                activity.awaitIdle();
                require(host.closeCompleted, "application cleanup did not complete");
            }
            default -> throw new AssertionError("unhandled local event: " + event);
        }
    }

    private void completion(JsonNode args, Context context) {
        if (args.has("invocation")) {
            fields(args, "invocation", "proxy_exited", "transaction", "lease");
            context.invocation = text(args, "invocation");
        } else {
            fields(args, "proxy_exited", "transaction", "lease");
            context.invocation = firstInvocation;
        }
        require(args.get("proxy_exited").isBoolean(), "invalid proxy milestone");
        boolean exited = args.get("proxy_exited").booleanValue();
        String transaction = text(args, "transaction");
        String lease = text(args, "lease");
        require(Set.of("committed", "rolled_back", "not_started", "unknown").contains(transaction), "unknown transaction milestone");
        require(Set.of("returned", "held", "not_acquired", "close_failed").contains(lease), "unknown lease milestone");
        Peer.Invocation invocation = invocationSnapshot(context.invocation);
        require(invocation != null, "unknown invocation");
        runTask(invocation.id);
        activity.awaitIdle();
        Peer.Proxy proxy;
        synchronized (peer) {
            proxy = invocation.proxy;
        }
        if (proxy != Peer.Proxy.SKIPPED) {
            applyTransaction(invocation, transaction);
            applyLease(invocation, lease);
            activity.awaitIdle();
            if (exited && !host.script(invocation.id).returned) {
                host.script(invocation.id).mailbox.put(new Fakes.ExitProxy());
                activity.awaitIdle();
            }
        }
        synchronized (peer) {
            Peer.Proxy observed = invocation.proxy;
            require(exited == (observed == Peer.Proxy.EXITED), "proxy milestone differs from observed dispatcher state");
            require(transaction.equals("not_started") == (invocation.transaction == Peer.Transaction.NONE),
                    "transaction milestone differs from observed state");
            require(lease.equals("not_acquired") == (invocation.lease == Peer.Lease.NOT_ACQUIRED),
                    "lease milestone differs from observed state");
        }
    }

    private void applyTransaction(Peer.Invocation invocation, String transaction) {
        Peer.Transaction target = switch (transaction) {
            case "committed" -> Peer.Transaction.COMMITTED;
            case "rolled_back" -> Peer.Transaction.ROLLED_BACK;
            case "unknown" -> Peer.Transaction.UNKNOWN;
            default -> Peer.Transaction.NONE;
        };
        Peer.Transaction current;
        synchronized (peer) {
            current = invocation.transaction;
        }
        if (current == target) {
            return;
        }
        require(target != Peer.Transaction.NONE, "transaction cannot become unstarted");
        require(!(target == Peer.Transaction.ROLLED_BACK && host.holdRollback), "held rollback never completes");
        if (current == Peer.Transaction.NONE) {
            peer.transactionBegun(invocation);
        }
        peer.transactionCompleted(invocation, target);
        if (target != Peer.Transaction.UNKNOWN) {
            host.databaseLock.signal();
        }
    }

    private void applyLease(Peer.Invocation invocation, String lease) {
        Peer.Lease current;
        synchronized (peer) {
            current = invocation.lease;
        }
        switch (lease) {
            case "not_acquired" -> require(current == Peer.Lease.NOT_ACQUIRED, "lease cannot be unacquired");
            case "held" -> {
                if (current == Peer.Lease.NOT_ACQUIRED) {
                    peer.leaseAcquired(invocation);
                }
            }
            case "returned" -> {
                if (current == Peer.Lease.NOT_ACQUIRED) {
                    peer.leaseAcquired(invocation);
                    current = Peer.Lease.HELD;
                }
                if (current == Peer.Lease.HELD) {
                    peer.leaseReturned(invocation);
                }
            }
            case "close_failed" -> {
                if (current == Peer.Lease.NOT_ACQUIRED) {
                    peer.leaseAcquired(invocation);
                    current = Peer.Lease.HELD;
                }
                if (current == Peer.Lease.HELD) {
                    peer.leaseCloseFailed(invocation);
                }
            }
            default -> throw new AssertionError("unknown lease milestone");
        }
    }

    private void advance(String anchor, JsonNode args) {
        fields(args, "elapsed_ms");
        require(args.get("elapsed_ms").isInt() && args.get("elapsed_ms").intValue() >= 0, "invalid elapsed time");
        Long origin = anchors.get(anchor);
        require(origin != null, "clock anchor was not observed");
        clock.advanceTo(origin + args.get("elapsed_ms").intValue());
        activity.awaitIdle();
    }

    static Throwable exception(JsonNode args) {
        fields(args, "invocation", "phase", "exception");
        require(PHASES.contains(text(args, "phase")), "unknown exception phase");
        JsonNode exception = args.get("exception");
        fields(exception, "class", "message", "vendor_code", "sql_state");
        String type = text(exception, "class");
        String message = text(exception, "message");
        require(EXCEPTIONS.contains(type), "unsupported exception class");
        require(exception.get("vendor_code").isInt(), "invalid vendor code");
        int vendor = exception.get("vendor_code").intValue();
        String state = text(exception, "sql_state");
        return switch (type) {
            case "java.sql.SQLException" -> new SQLException(message, state, vendor);
            default -> {
                require(vendor == 0 && state.isEmpty(), "non-SQL exceptions use vendor code 0 and empty SQLSTATE");
                yield type.equals("java.lang.IllegalStateException") ? new IllegalStateException(message)
                        : new RuntimeException(message);
            }
        };
    }

    private Peer.Invocation live(String id, String worker) {
        Peer.Invocation invocation = invocationSnapshot(id);
        require(invocation != null && (worker == null || invocation.worker.equals(worker)), "unknown invocation identity");
        return invocation;
    }

    private void runTask(String invocation) {
        threads.run(invocation);
        activity.awaitIdle();
    }

    // -------------------------------------------------------------- assertions

    void observe(String label, Context c) {
        synchronized (peer) {
            Peer.Invocation invocation = c.invocation == null ? null : peer.invocations.get(c.invocation);
            List<JsonNode> emitted = output.frames.subList(c.outputsBefore, output.frames.size());
            switch (label) {
                case "initialize_application" -> need(label, host.initializeCalls == 1 && host.initializationStarted);
                case "validate_registration" -> need(label, host.validated);
                case "probe_database" -> need(label, host.probeStarted);
                case "send_ready" -> need(label, count(emitted, "ready", null) == 1);
                case "send_arrive" -> need(label, count(emitted, "arrive", c.invocation) == 1
                        && emitted(emitted, "arrive").get("body").get("arrival").stringValue().equals(c.arrival));
                case "send_terminal" -> need(label, count(emitted, "terminal", c.invocation) == 1);
                case "send_stopped" -> need(label, count(emitted, "stopped", null) == 1);
                case "reserve_invocation" -> need(label, invocation != null && !invocation.retired
                        && peer.workers.get(invocation.worker) == invocation);
                case "dispatch_once" -> need(label, invocation != null && invocation.dispatches == 1
                        && threads.submissions(invocation.id) == 1);
                case "install_gate" -> need(label, invocation != null && invocation.gate != null
                        && invocation.gate.arrival.equals(c.arrival) && invocation.gate.point.equals(c.point)
                        && invocation.gate.state == Peer.GateState.WAITING && invocation.gate.parked());
                case "wake_exact_gate" -> {
                    String arrival = c.frame.get("body").get("arrival").stringValue();
                    Peer.Gate gate = gate(invocation, arrival);
                    need(label, gate != null && gate.state == Peer.GateState.RELEASED && !gate.parked()
                            && invocation.gate != gate && resumed(invocation, gate)
                            && otherGatesWaiting(invocation));
                }
                case "no_terminal" -> need(label, count(emitted, "terminal", null) == 0);
                case "no_stopped" -> need(label, count(output.frames, "stopped", null) == 0);
                case "no_ready" -> need(label, count(output.frames, "ready", null) == 0);
                case "no_reply" -> need(label, emitted.isEmpty());
                case "cancel_latched" -> {
                    if (c.frame.get("type").stringValue().equals("fatal")) {
                        need(label, !peer.invocations.isEmpty() && peer.invocations.values().stream()
                                .allMatch(i -> i.retired || i.cancelReason != null));
                    } else {
                        need(label, invocation != null
                                && c.frame.get("body").get("reason").stringValue().equals(invocation.cancelReason));
                    }
                }
                case "wake_gate_exceptionally" -> {
                    List<Peer.Invocation> targets = targets(c, invocation);
                    need(label, !targets.isEmpty() && targets.stream().allMatch(i -> !i.gates.isEmpty()
                            && i.gates.getLast().state == Peer.GateState.CANCELLED && i.gate == null
                            && host.script(i.id).cancelObserved != null));
                }
                case "arm_cleanup_watchdog" -> {
                    if (c.frame.get("type").stringValue().equals("fatal")) {
                        need(label, pending(peer.fatalWatchdog)
                                && peer.fatalDeadline == clock.nowMillis() + peer.start.cancelMillis());
                    } else {
                        need(label, invocation != null && pending(invocation.cancelWatchdog)
                                && invocation.cancelDeadline == clock.nowMillis() + peer.start.cancelMillis());
                    }
                }
                case "request_jdbc_cancel" -> {
                    List<Peer.Invocation> targets = targets(c, invocation);
                    need(label, !targets.isEmpty() && targets.stream().allMatch(i -> i.jdbcCancelRequests == 1));
                }
                case "close_admission" -> need(label, peer.admissionClosed);
                case "retain_earlier_cleanup_deadline" -> need(label, peer.invocations.values().stream()
                        .anyMatch(i -> !i.retired && pending(i.cancelWatchdog) && i.cancelDeadline < peer.stopDeadline)
                        && pending(peer.stopWatchdog));
                case "await_active_cleanup" -> need(label, peer.live > 0 && !host.closeStarted && !peer.cleanupStarted);
                case "no_child_exit" -> need(label, exit.status() == null);
                case "retire_invocation" -> need(label, invocation != null && invocation.retired
                        && peer.workers.get(invocation.worker) != invocation);
                case "no_resume" -> need(label, invocation != null && progressCount() == c.progressBefore);
                case "force_nonzero_exit", "exit_nonzero" -> need(label, exit.status() != null && exit.status() == 1
                        && exit.calls == 1);
                case "record_source_exception" -> need(label, invocation != null
                        && invocation.source == injected.get(invocation.id));
                case "cleanup_still_blocked" -> need(label, exit.status() == null
                        && (peer.invocations.values().stream().anyMatch(i -> !i.retired) || (host.closeStarted && !host.closeCompleted)));
                case "no_cleanup_success" -> need(label, count(output.frames, "stopped", null) == 0
                        && !host.closeCompleted && !peer.applicationClosed);
                case "fatal_protocol", "fatal_cleanup", "fatal_version", "fatal_startup", "fatal_shutdown", "fatal_transaction" -> {
                    String kind = label.substring("fatal_".length());
                    need(label, kind.equals(peer.fatalKind) && peer.fatalSent && emitted.stream()
                            .anyMatch(f -> f.get("type").stringValue().equals("fatal")
                                    && f.get("body").get("kind").stringValue().equals(kind)));
                }
                case "rollback_barrier_armed" -> need(label, host.holdRollback);
                case "close_pool_and_application" -> need(label, host.closeCompleted && peer.applicationClosed);
                case "consume_retired_invocation" -> need(label, invocation != null && invocation.retired
                        && peer.received == c.frame.get("seq").intValue() && peer.fatalKind == null
                        && emitted.isEmpty());
                case "begin_bounded_cleanup" -> need(label, peer.phase == Peer.Phase.FAILED
                        && (pending(peer.fatalWatchdog) || pending(peer.stopWatchdog)));
                case "no_arrive_for_w2" -> {
                    Peer.Invocation w2 = peer.workers.get("w2");
                    need(label, w2 != null && host.script(w2.id).blockedInJdbc && output.frames.stream()
                            .noneMatch(f -> f.get("type").stringValue().equals("arrive")
                                    && f.get("body").get("worker").stringValue().equals("w2")));
                }
                case "database_lock_released" -> {
                    Peer.Invocation w2 = peer.workers.get("w2");
                    need(label, host.databaseLock.signalled() && w2 != null && host.script(w2.id).jdbcResumed
                            && !host.script(w2.id).blockedInJdbc);
                }
                case "ignore_cancelled_gate" -> {
                    Peer.Gate gate = gate(invocation, c.frame.get("body").get("arrival").stringValue());
                    need(label, gate != null && gate.state == Peer.GateState.CANCELLED
                            && peer.received == c.frame.get("seq").intValue() && peer.fatalKind == null);
                }
                case "prevent_command_start" -> need(label, invocation != null && invocation.cancelReason != null
                        && invocation.proxy == Peer.Proxy.NOT_ENTERED && !threads.started.containsKey(invocation.id));
                case "committed_terminal_unchanged" -> need(label, invocation != null && invocation.terminal != null
                        && invocation.terminal.get("transaction").stringValue().equals("committed")
                        && invocation.terminal.get("error").isNull() && invocation.transaction == Peer.Transaction.COMMITTED);
                case "no_second_terminal" -> need(label, invocation != null && invocation.terminals == 1
                        && count(emitted, "terminal", null) == 0);
                case "no_rollback" -> need(label, invocation != null && invocation.transaction == Peer.Transaction.COMMITTED
                        && invocation.cancelReason == null);
                case "no_fatal" -> need(label, peer.fatalKind == null && peer.phase != Peer.Phase.FAILED);
                case "no_command_start" -> need(label, host.initializeCalls == 0 && threads.submitted.isEmpty()
                        && peer.start == null);
                case "cancel_startup" -> need(label, host.cancelStartupCalls >= 1 && peer.startupCancelled);
                case "close_control_stream" -> need(label, output.closed && peer.outputClosed);
                case "initialization_barrier_armed" -> need(label, host.holdInitialization);
                case "arm_startup_watchdog" -> need(label, pending(peer.startupWatchdog)
                        && peer.startupDeadline == anchors.get("startup") + c.frame.get("body").get("startup_ms").intValue());
                case "initialization_blocked", "initialization_still_blocked" -> need(label, host.initializationStarted
                        && !host.initializationReturned && host.initialization.parked() && exit.status() == null);
                case "application_shutdown_barrier_armed" -> need(label, host.holdShutdown);
                case "arm_stop_watchdog" -> need(label, pending(peer.stopWatchdog)
                        && peer.stopDeadline == anchors.get("stop") + c.frame.get("body").get("budget_ms").intValue());
                case "application_cleanup_blocked" -> need(label, host.closeStarted && !host.closeCompleted
                        && host.shutdown.parked());
                case "ignore_duplicate" -> need(label, peer.received == c.receivedBefore && peer.fatalKind == null);
                case "no_redispatch" -> need(label, invocation != null && invocation.dispatches == 1
                        && threads.submissions(invocation.id) == 1);
                case "outbound_sequence_at_limit" -> need(label, peer.sent == Wire.MAX_SEQ && peer.fatalKind == null);
                case "arrival_sequence_at_limit" -> need(label, invocation != null && invocation.arrivals == Wire.MAX_ARRIVAL
                        && invocation.gate == null);
                case "fatal_without_wire" -> need(label, peer.fatalKind != null && !peer.fatalSent
                        && invocation != null && invocation.dispatches == 0 && threads.submissions(invocation.id) == 0);
                default -> throw new AssertionError("unhandled assertion " + label);
            }
        }
    }

    /** Released gates plus scripted resumptions across every invocation. */
    private long progressCount() {
        synchronized (peer) {
            long released = peer.invocations.values().stream()
                    .flatMap(i -> i.gates.stream()).filter(g -> g.state == Peer.GateState.RELEASED).count();
            long resumed;
            synchronized (host) {
                resumed = host.scripts.values().stream().mapToLong(s -> s.resumed.size()).sum();
            }
            return released + resumed;
        }
    }

    private List<Peer.Invocation> targets(Context c, Peer.Invocation invocation) {
        if (c.frame != null && c.frame.get("type").stringValue().equals("fatal")) {
            List<Peer.Invocation> all = new ArrayList<>(peer.invocations.values());
            all.removeIf(i -> i.retired && i.cancelReason == null);
            return all;
        }
        return invocation == null ? List.of() : List.of(invocation);
    }

    private boolean resumed(Peer.Invocation invocation, Peer.Gate gate) {
        if (invocation == null || gate == null) {
            return false;
        }
        long passed = invocation.gates.stream().filter(g -> g.state == Peer.GateState.RELEASED).count();
        return host.script(invocation.id).resumed.size() == passed && gate.state == Peer.GateState.RELEASED;
    }

    private boolean otherGatesWaiting(Peer.Invocation released) {
        for (Peer.Invocation other : peer.invocations.values()) {
            if (other != released && other.gate != null && (other.gate.state != Peer.GateState.WAITING || !other.gate.parked())) {
                return false;
            }
        }
        return true;
    }

    private static Peer.Gate gate(Peer.Invocation invocation, String arrival) {
        if (invocation == null) {
            return null;
        }
        for (Peer.Gate gate : invocation.gates) {
            if (gate.arrival.equals(arrival)) {
                return gate;
            }
        }
        return null;
    }

    private static boolean pending(Seams.Timer timer) {
        return timer instanceof Fakes.Clock.Timer fake && fake.pending();
    }

    private static int count(List<JsonNode> frames, String type, String invocation) {
        int n = 0;
        for (JsonNode frame : frames) {
            if (frame.get("type").stringValue().equals(type) && (invocation == null
                    || frame.get("body").get("invocation").stringValue().equals(invocation))) {
                n++;
            }
        }
        return n;
    }

    private static JsonNode emitted(List<JsonNode> frames, String type) {
        return frames.stream().filter(f -> f.get("type").stringValue().equals(type)).findFirst().orElseThrow();
    }

    private void need(String label, boolean ok) {
        if (!ok) {
            throw new AssertionError(row + ": unmet vector assertion " + label);
        }
    }

    static void fields(JsonNode node, String... names) {
        require(node != null && node.isObject() && node.size() == names.length, "unexpected event argument fields");
        for (String name : names) {
            require(node.has(name), "missing event argument " + name);
        }
        Iterator<String> actual = node.propertyNames().iterator();
        while (actual.hasNext()) {
            require(Set.of(names).contains(actual.next()), "unknown event argument");
        }
    }

    static String text(JsonNode node, String field) {
        JsonNode value = node.get(field);
        require(value != null && value.isString(), "invalid event argument " + field);
        return value.stringValue();
    }

    static void require(boolean ok, String message) {
        if (!ok) {
            throw new AssertionError(message);
        }
    }
}
