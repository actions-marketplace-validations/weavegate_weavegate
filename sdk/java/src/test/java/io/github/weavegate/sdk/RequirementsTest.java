package io.github.weavegate.sdk;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.io.InputStream;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.stream.Collectors;

import java.util.stream.IntStream;
import java.util.stream.Stream;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.TestFactory;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * Acceptance-family tests that do not depend on Spring or MySQL. Each repeated
 * method reports its requirement row once per repetition, only after all of its
 * assertions have passed.
 */
class RequirementsTest {
    private static final String HANDLER = "sdk/java/src/test/java/io/github/weavegate/sdk/RequirementsTest.java:RequirementsTest.";
    private static final Vectors VECTORS = Vectors.load();

    interface Body {
        void run() throws Exception;
    }

    /** One JUnit execution per repetition, so evidence is attributed to individual passes. */
    static Stream<DynamicTest> repeated(Body body) {
        return IntStream.rangeClosed(1, Vectors.repetitions())
                .mapToObj(n -> DynamicTest.dynamicTest("repetition " + n, body::run));
    }

    /** Unknown event names, argument fields, assertions and exception shapes are rejected before injection. */
    @TestFactory
    Stream<DynamicTest> javaDispatchIsClosed() {
        return repeated(() -> {
            JsonNode success = VECTORS.javaCases().getFirst();
            List<JsonNode> steps = VECTORS.steps(success);

            VectorHarness unknownEvent = new VectorHarness("dispatch").quiet();
            ObjectNode event = Vectors.JSON.createObjectNode();
            event.put("peer", "java");
            event.put("action", "local");
            event.put("event", "invented_event");
            event.set("args", Vectors.JSON.createObjectNode());
            event.set("expect", Vectors.JSON.createArrayNode());
            assertThatThrownBy(() -> unknownEvent.step(0, event)).hasMessageContaining("unhandled local event");
            assertThat(unknownEvent.output.frames).isEmpty();

            VectorHarness extraArgument = new VectorHarness("dispatch").quiet();
            extraArgument.step(0, steps.get(0));
            ObjectNode readiness = (ObjectNode) steps.get(1).deepCopy();
            ((ObjectNode) readiness.get("args")).put("unexpected", true);
            assertThatThrownBy(() -> extraArgument.step(1, readiness)).hasMessageContaining("event argument");
            assertThat(extraArgument.host.probe.signalled()).isFalse();
            assertThat(extraArgument.output.frames).isEmpty();

            VectorHarness unknownAssertion = new VectorHarness("dispatch").quiet();
            ObjectNode start = (ObjectNode) steps.get(0).deepCopy();
            start.set("expect", Vectors.JSON.createArrayNode().add("invented_assertion"));
            assertThatThrownBy(() -> unknownAssertion.step(0, start)).hasMessageContaining("unhandled assertion");
            assertThat(unknownAssertion.host.initializeCalls).isZero();
            assertThat(unknownAssertion.output.frames).isEmpty();

            VectorHarness unknownPeer = new VectorHarness("dispatch").quiet();
            ObjectNode peer = (ObjectNode) steps.get(0).deepCopy();
            peer.put("peer", "python");
            assertThatThrownBy(() -> unknownPeer.step(0, peer)).hasMessageContaining("unknown vector peer");
            assertThat(unknownPeer.host.initializeCalls).isZero();

            ObjectNode withdrawn = (ObjectNode) steps.get(0).deepCopy();
            withdrawn.put("action", "withdraw");
            VectorHarness unknownAction = new VectorHarness("dispatch").quiet();
            assertThatThrownBy(() -> unknownAction.step(0, withdrawn)).hasMessageContaining("unknown vector action");
            assertThat(unknownAction.host.initializeCalls).isZero();
            report("java-dispatch");
        });
    }

    /** command_exception accepts exactly the three phases and declared exception fields. */
    @TestFactory
    Stream<DynamicTest> commandExceptionShapeIsClosed() {
        return repeated(() -> {
            for (String phase : List.of("command_body", "jdbc_operation", "after_commit_callback")) {
                assertThat(VectorHarness.exception(exception(phase, "java.lang.IllegalStateException", 0, "")))
                        .isInstanceOf(IllegalStateException.class);
            }
            assertThat(VectorHarness.exception(exception("jdbc_operation", "java.sql.SQLException", 1213, "40001")))
                    .isInstanceOf(java.sql.SQLException.class)
                    .satisfies(e -> assertThat(((java.sql.SQLException) e).getErrorCode()).isEqualTo(1213));
            for (ObjectNode invalid : List.of(exception("commit", "java.lang.IllegalStateException", 0, ""),
                    exception("command_body", "java.lang.Error", 0, ""),
                    exception("command_body", "java.lang.IllegalStateException", 1213, "40001"))) {
                VectorHarness harness = Scripted.ready(1);
                harness.peer.receive(Scripted.invoke(2, Scripted.I1, "w1", "assign"));
                ObjectNode args = invalid.deepCopy();
                assertThatThrownBy(() -> harness.local("command_exception", args)).isInstanceOf(AssertionError.class);
                assertThat(harness.threads.started).isEmpty();
                assertThat(harness.peer.invocations.get(Scripted.I1).source).isNull();
            }
            ObjectNode extra = exception("command_body", "java.lang.IllegalStateException", 0, "");
            ((ObjectNode) extra.get("exception")).put("stack", "x");
            assertThatThrownBy(() -> VectorHarness.exception(extra)).hasMessageContaining("event argument");
            report("command-exception-shape");
        });
    }

    private static ObjectNode exception(String phase, String type, int vendor, String state) {
        ObjectNode args = Vectors.JSON.createObjectNode();
        args.put("invocation", Scripted.I1);
        args.put("phase", phase);
        ObjectNode exception = args.putObject("exception");
        exception.put("class", type);
        exception.put("message", "synthetic");
        exception.put("vendor_code", vendor);
        exception.put("sql_state", state);
        return args;
    }

    /** The first delivered cancellation origin determines the terminal message. */
    @TestFactory
    Stream<DynamicTest> cancellationOriginIsFirstLatched() {
        return repeated(() -> {
            assertThat(cancelThenStop("context")).isEqualTo("cancelled by context");
            assertThat(cancelThenStop("stop")).isEqualTo("cancelled by stop");
            assertThat(cancelThenStop(null)).isEqualTo("cancelled by stop");

            VectorHarness twice = Scripted.ready(1);
            twice.peer.receive(Scripted.invoke(2, Scripted.I1, "w1", "assign"));
            twice.peer.receive(Scripted.cancel(3, Scripted.I1, "w1", "context"));
            twice.peer.receive(Scripted.cancel(4, Scripted.I1, "w1", "stop"));
            assertThat(twice.peer.invocations.get(Scripted.I1).cancelReason).isEqualTo("context");
            assertThat(twice.peer.fatalKind).isNull();
            report("java-cancel-origin");
        });
    }

    private static String cancelThenStop(String reason) {
        VectorHarness h = Scripted.ready(1);
        h.peer.receive(Scripted.invoke(2, Scripted.I1, "w1", "assign"));
        h.threads.run(Scripted.I1);
        h.activity.awaitIdle();
        h.host.script(Scripted.I1).mailbox.put(new Fakes.Arrive("after_read"));
        h.activity.awaitIdle();
        int seq = 3;
        if (reason != null) {
            h.peer.receive(Scripted.cancel(seq++, Scripted.I1, "w1", reason));
        }
        h.peer.receive(Scripted.stop(seq, 2500));
        h.activity.awaitIdle();
        Peer.Invocation inv = h.peer.invocations.get(Scripted.I1);
        h.peer.transactionBegun(inv);
        h.peer.leaseAcquired(inv);
        h.peer.transactionCompleted(inv, Peer.Transaction.ROLLED_BACK);
        h.peer.leaseReturned(inv);
        h.host.script(Scripted.I1).mailbox.put(new Fakes.ExitProxy());
        h.activity.awaitIdle();
        JsonNode terminal = h.output.frames.stream().filter(f -> f.get("type").stringValue().equals("terminal"))
                .findFirst().orElseThrow();
        return terminal.get("body").get("error").get("message").stringValue();
    }

    /** Wire matrix checks plus the reviewed shared adversarial case inventory. */
    @TestFactory
    Stream<DynamicTest> wireMatrixRejectsInvalidInputWithoutApplicationEffects() {
        return repeated(() -> {
            // Coalesced frames in one read are dispatched in order.
            VectorHarness coalesced = new VectorHarness("matrix").quiet();
            byte[] start = Scripted.start(1);
            byte[] stop = Scripted.stop(2, 2500);
            java.io.ByteArrayOutputStream both = new java.io.ByteArrayOutputStream();
            for (byte[] payload : List.of(start, stop)) {
                both.write(payload.length >>> 24);
                both.write(payload.length >>> 16);
                both.write(payload.length >>> 8);
                both.write(payload.length);
                both.writeBytes(payload);
            }
            coalesced.host.shutdown.signal();
            // No EOF here: EOF before verified shutdown is itself a transport fault.
            FrameReader reader = new FrameReader(new java.io.ByteArrayInputStream(both.toByteArray()));
            assertThat(coalesced.peer.receive(reader.next())).isTrue();
            assertThat(coalesced.peer.receive(reader.next())).isTrue();
            coalesced.activity.awaitIdle();
            assertThat(types(coalesced)).containsExactly("stopped");
            assertThat(coalesced.exit.status()).isZero();

            // A transport that returns exactly one byte for every read still
            // assembles one frame and publishes ready only after the payload.
            byte[] single = Scripted.start(1);
            java.io.ByteArrayOutputStream framed = new java.io.ByteArrayOutputStream();
            for (int shift : List.of(24, 16, 8, 0)) {
                framed.write(single.length >>> shift);
            }
            framed.writeBytes(single);
            java.io.ByteArrayInputStream bytes = new java.io.ByteArrayInputStream(framed.toByteArray()) {
                @Override
                public synchronized int read(byte[] target, int offset, int length) {
                    return super.read(target, offset, Math.min(length, 1));
                }
            };
            FrameReader everyByte = new FrameReader(bytes);
            VectorHarness fragmented = new VectorHarness("matrix").quiet();
            assertThat(fragmented.peer.receive(everyByte.next())).isTrue();
            fragmented.activity.awaitIdle();
            assertThat(types(fragmented)).isEmpty();
            fragmented.host.completeProbe();
            fragmented.activity.awaitIdle();
            assertThat(types(fragmented)).containsExactly("ready");
            assertThat(everyByte.next()).isNull();

            assertFatal(h -> h.peer.receive(Scripted.frame("ready", 1, Map.of("commands", List.of("assign"),
                    "points", List.of(), "capacity", 1))), false, "protocol");
            assertFatal(h -> h.peer.receive(Scripted.invoke(2, Scripted.I1, "w1", "unknown_command")), true, "protocol");
            assertFatal(h -> {
                h.peer.receive(Scripted.invoke(2, Scripted.I1, "w1", "assign"));
                h.peer.receive(Scripted.cancel(3, Scripted.I1, "w2", "context"));
            }, true, "protocol");
            assertFatal(h -> {
                h.peer.receive(Scripted.invoke(2, Scripted.I1, "w1", "assign"));
                h.peer.receive(Scripted.invoke(3, Scripted.I2, "w2", "assign"));
            }, true, "protocol");
            assertFatal(h -> {
                h.peer.receive(Scripted.invoke(2, Scripted.I1, "w1", "assign"));
                h.peer.receive(Scripted.invoke(3, Scripted.I1, "w1", "assign"));
            }, true, "protocol");
            // An exact duplicate start is ignored; the same start under a new sequence is fatal.
            VectorHarness exact = Scripted.ready(1);
            exact.peer.receive(Scripted.start(1));
            assertThat(exact.peer.fatalKind).isNull();
            assertFatal(h -> h.peer.receive(Scripted.start(2, 1)), true, "protocol");
            assertFatal(h -> h.peer.receive(Scripted.invoke(3, Scripted.I1, "w1", "assign")), true, "protocol");

            VectorHarness exhaustedSequence = Scripted.ready(1);
            exhaustedSequence.peer.sent = Wire.MAX_SEQ;
            exhaustedSequence.peer.receive(Scripted.invoke(2, Scripted.I1, "w1", "assign"));
            assertThat(exhaustedSequence.peer.fatalKind).isEqualTo("protocol");
            assertThat(exhaustedSequence.peer.invocations.get(Scripted.I1).dispatches).isZero();

            VectorHarness exhaustedArrival = Scripted.ready(1);
            exhaustedArrival.peer.receive(Scripted.invoke(2, Scripted.I1, "w1", "assign"));
            Peer.Invocation arrivalInvocation = exhaustedArrival.peer.invocations.get(Scripted.I1);
            arrivalInvocation.arrivals = Wire.MAX_ARRIVAL;
            exhaustedArrival.threads.run(Scripted.I1);
            exhaustedArrival.activity.awaitIdle();
            exhaustedArrival.host.script(Scripted.I1).mailbox.put(new Fakes.Arrive("after_read"));
            exhaustedArrival.activity.awaitIdle();
            assertThat(exhaustedArrival.peer.fatalKind).isEqualTo("protocol");
            assertThat(types(exhaustedArrival)).doesNotContain("arrive");
            assertFatal(h -> {
                h.peer.receive(Scripted.invoke(2, Scripted.I1, "w1", "assign"));
                h.threads.run(Scripted.I1);
                h.activity.awaitIdle();
                h.host.script(Scripted.I1).mailbox.put(new Fakes.Arrive("unregistered_point"));
                h.activity.awaitIdle();
            }, true, "protocol");

            // Released-arrival duplicates and retired cancels are consumed without effects.
            VectorHarness h = Scripted.ready(2);
            h.peer.receive(Scripted.invoke(2, Scripted.I1, "w1", "assign"));
            h.threads.run(Scripted.I1);
            h.activity.awaitIdle();
            h.host.script(Scripted.I1).mailbox.put(new Fakes.Arrive("after_read"));
            h.activity.awaitIdle();
            h.peer.receive(Scripted.release(3, Scripted.I1, "w1", "1", "after_read"));
            h.activity.awaitIdle();
            h.peer.receive(Scripted.release(4, Scripted.I1, "w1", "1", "after_read"));
            h.activity.awaitIdle();
            assertThat(h.host.script(Scripted.I1).resumed).containsExactly("after_read");
            Scripted.complete(h, Scripted.I1, Peer.Transaction.COMMITTED);
            h.peer.receive(Scripted.cancel(5, Scripted.I1, "w1", "context"));
            assertThat(h.peer.fatalKind).isNull();
            assertThat(types(h)).containsExactly("ready", "accepted", "arrive", "terminal");

            // Stale sessions are dropped without advancing sequence state.
            h.peer.receive(Scripted.frame("stop", Scripted.RUN, "5".repeat(32), 6, Map.of("budget_ms", 1)));
            assertThat(h.peer.staleFrames).isEqualTo(1);
            assertThat(h.peer.received).isEqualTo(5);

            JsonNode matrix = VECTORS.data.get("coverage").get("java_wire_matrix");
            assertThat(matrix.size()).isEqualTo(12);
            for (JsonNode name : matrix) {
                JsonNode vector = VECTORS.javaCases().stream()
                        .filter(c -> c.get("id").stringValue().equals(name.stringValue()))
                        .findFirst().orElseThrow();
                new VectorHarness("independent").quiet().run(VECTORS.steps(vector));
            }
            report("java-wire-matrix");
        });
    }

    @TestFactory
    Stream<DynamicTest> arrivalsBelongToTheInvokedCommand() {
        return repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            h.peer.receive(Scripted.frame("start", 1, Map.of("variant", "fixed", "params", Map.of(),
                    "commands", List.of("assign", "other"), "points", List.of("after_read", "before_write"),
                    "capacity", 1, "database", Map.of("driver", "mysql", "host", "127.0.0.1", "port", 33060,
                            "name", "weavegate", "username", "synthetic", "password", "synthetic-only"),
                    "startup_ms", 10000, "cancel_ms", 1000)));
            h.activity.awaitIdle();
            h.host.completeProbe();
            h.activity.awaitIdle();
            h.peer.receive(Scripted.invoke(2, Scripted.I1, "w1", "other"));
            h.threads.run(Scripted.I1);
            h.activity.awaitIdle();
            h.host.script(Scripted.I1).mailbox.put(new Fakes.Arrive("before_write"));
            h.activity.awaitIdle();
            h.peer.receive(Scripted.cancel(3, Scripted.I1, "w1", "context"));
            h.host.script(Scripted.I1).mailbox.put(new Fakes.ExitProxy());
            h.activity.awaitIdle();
            assertThat(h.peer.fatalKind).isEqualTo("protocol");

            VectorHarness requested = new VectorHarness("independent").quiet();
            requested.peer.receive(Scripted.frame("start", 1, Map.of("variant", "fixed", "params", Map.of(),
                    "commands", List.of("assign"), "points", List.of("after_read"),
                    "capacity", 1, "database", Map.of("driver", "mysql", "host", "127.0.0.1", "port", 33060,
                            "name", "weavegate", "username", "synthetic", "password", "synthetic-only"),
                    "startup_ms", 10000, "cancel_ms", 1000)));
            requested.activity.awaitIdle();
            requested.host.completeProbe();
            requested.activity.awaitIdle();
            requested.peer.receive(Scripted.invoke(2, Scripted.I1, "w1", "assign"));
            requested.threads.run(Scripted.I1);
            requested.activity.awaitIdle();
            requested.host.script(Scripted.I1).mailbox.put(new Fakes.Arrive("before_write"));
            requested.activity.awaitIdle();
            requested.peer.receive(Scripted.cancel(3, Scripted.I1, "w1", "context"));
            requested.host.script(Scripted.I1).mailbox.put(new Fakes.ExitProxy());
            requested.activity.awaitIdle();
            assertThat(requested.peer.fatalKind).isEqualTo("protocol");
        });
    }

    @TestFactory
    Stream<DynamicTest> foreignOutboundFramesAreDroppedBeforeDirectionChecks() {
        return repeated(() -> {
            VectorHarness h = Scripted.ready(1);
            String foreign = "5".repeat(32);
            Map<String, Object> terminal = new LinkedHashMap<>();
            terminal.put("invocation", Scripted.I1);
            terminal.put("worker", "w1");
            terminal.put("transaction", "committed");
            terminal.put("connection", "returned");
            terminal.put("error", null);
            List<byte[]> frames = List.of(
                    Scripted.frame("ready", Scripted.RUN, foreign, 2,
                            Map.of("commands", List.of("assign"), "points", List.of("after_read"), "capacity", 1)),
                    Scripted.frame("accepted", Scripted.RUN, foreign, 2,
                            Map.of("invocation", Scripted.I1, "worker", "w1")),
                    Scripted.frame("arrive", Scripted.RUN, foreign, 2,
                            Map.of("invocation", Scripted.I1, "worker", "w1", "arrival", "1", "point", "after_read")),
                    Scripted.frame("terminal", Scripted.RUN, foreign, 2, terminal),
                    Scripted.frame("stopped", Scripted.RUN, foreign, 2, Map.of()));
            for (byte[] frame : frames) {
                assertThat(h.peer.receive(frame)).isTrue();
            }
            assertThat(h.peer.fatalKind).isNull();
            assertThat(h.peer.staleFrames).isEqualTo(frames.size());
            assertThat(h.peer.received).isEqualTo(1);
            assertThat(types(h)).containsExactly("ready");
        });
    }

    private interface Action {
        void run(VectorHarness h);
    }

    private static void assertFatal(Action action, boolean ready, String kind) {
        VectorHarness h = ready ? Scripted.ready(1) : new VectorHarness("matrix").quiet();
        action.run(h);
        h.activity.awaitIdle();
        assertThat(h.peer.fatalKind).isEqualTo(kind);
        assertThat(h.output.frames.getLast().get("type").stringValue()).isEqualTo("fatal");
        assertThat(h.output.frames.stream().filter(f -> f.get("type").stringValue().equals("terminal"))).isEmpty();
    }

    private static List<String> types(VectorHarness h) {
        return h.output.frames.stream().map(f -> f.get("type").stringValue()).collect(Collectors.toList());
    }

    /** Inactive sync points return immediately without input, protocol threads or transaction changes. */
    @TestFactory
    Stream<DynamicTest> disabledInstrumentationIsInert() {
        return repeated(() -> {
            InputStream original = System.in;
            AtomicBoolean read = new AtomicBoolean();
            System.setIn(new InputStream() {
                @Override
                public int read() {
                    read.set(true);
                    throw new AssertionError("inactive instrumentation read stdin");
                }
            });
            try {
                Set<String> before = weavegateThreads();
                assertThat(Weavegate.active()).isFalse();
                boolean synchronizationBefore = TransactionSynchronizationManager.isSynchronizationActive();
                Map<Object, Object> resourcesBefore = TransactionSynchronizationManager.getResourceMap();
                Weavegate.syncPoint("after_read");
                Weavegate.syncPoint("not registered anywhere");
                assertThat(read).isFalse();
                assertThat(weavegateThreads()).isEqualTo(before);
                assertThat(TransactionSynchronizationManager.isSynchronizationActive()).isEqualTo(synchronizationBefore);
                assertThat(TransactionSynchronizationManager.getResourceMap()).isEqualTo(resourcesBefore);
                assertThat(Peer.current()).isNull();
                report("disabled-instrumentation");
            } finally {
                System.setIn(original);
            }
        });
    }

    private static Set<String> weavegateThreads() {
        return Thread.getAllStackTraces().keySet().stream().map(Thread::getName)
                .filter(name -> name.startsWith("weavegate-")).collect(Collectors.toSet());
    }

    @AfterAll
    static void markers() {
        EvidenceListener.marker("EXTERNAL_SUT_JAVA_DISPATCH_RESULT events=closed arguments=closed assertions=closed injection=none");
        EvidenceListener.marker("EXTERNAL_SUT_JAVA_EXCEPTION_SHAPE_RESULT phases=3 unknown_phase=rejected injection=none");
        EvidenceListener.marker("EXTERNAL_SUT_JAVA_CANCEL_ORIGIN_RESULT context_then_stop=context stop_only=stop first_latched=retained");
        EvidenceListener.marker("EXTERNAL_SUT_JAVA_WIRE_MATRIX_RESULT coalesced=ordered invalid=fatal duplicates=consumed acceptance=complete");
        EvidenceListener.marker("EXTERNAL_SUT_JAVA_DISABLED_RESULT sync_point=immediate stdin=unread protocol_threads=none transaction=unchanged");
    }

    private static void report(String requirement) {
        EvidenceListener.check("requirement/" + requirement, "observe/evidence", HANDLER + caller());
    }

    private static String caller() {
        // Lambda bodies compile to synthetic methods; attribute evidence to the declaring factory.
        return StackWalker.getInstance().walk(frames -> frames.skip(2)
                .map(StackWalker.StackFrame::getMethodName)
                .map(name -> name.startsWith("lambda$") ? name.split("\\$")[1] : name)
                .findFirst().orElseThrow());
    }
}
