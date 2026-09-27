package io.github.weavegate.sdk;

import static org.assertj.core.api.Assertions.assertThat;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.TimeUnit;
import java.util.function.Predicate;
import java.util.stream.Stream;

import com.zaxxer.hikari.HikariDataSource;
import io.github.weavegate.sdk.fixture.FixtureApplication;
import io.github.weavegate.sdk.fixture.Journal;
import io.github.weavegate.sdk.fixture.SeatCommandApi;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.TestFactory;
import org.testcontainers.mysql.MySQLContainer;
import tools.jackson.databind.JsonNode;

/**
 * Pinned Spring transaction manager, HikariCP and Connector/J against MySQL 8.4.
 * Journal events come from the driver wrapper and command body, independently
 * of the SDK's own milestone records.
 */
class SpringTransactionsTest {
    private static final String HANDLER = "sdk/java/src/test/java/io/github/weavegate/sdk/SpringTransactionsTest.java:SpringTransactionsTest.";
    private static final MySQLContainer MYSQL = new MySQLContainer("mysql:8.4").withDatabaseName("weavegate")
            .withUsername("synthetic").withPassword("synthetic-only");

    @BeforeAll
    static void startDatabase() throws Exception {
        MYSQL.start();
        try (Connection c = admin(); Statement s = c.createStatement()) {
            s.execute("CREATE TABLE seat (id INT PRIMARY KEY, taken_by VARCHAR(64))");
        }
    }

    @AfterAll
    static void stopDatabase() {
        MYSQL.stop();
        EvidenceListener.marker("EXTERNAL_SUT_JAVA_SPRING_RESULT terminal=after_proxy_commit_close begin_failure=not_started "
                + "rollback_only=rolled_back after_commit=committed_error unknown_commit=fatal unknown_rollback=fatal "
                + "suppressed_close=fatal cancel=rolled_back jdbc_cancel=requested");
    }

    private static Connection admin() throws Exception {
        return DriverManager.getConnection(MYSQL.getJdbcUrl(), "root", MYSQL.getPassword());
    }

    private static void resetSeat() throws Exception {
        try (Connection c = admin(); Statement s = c.createStatement()) {
            s.execute("DELETE FROM seat");
            s.execute("INSERT INTO seat (id, taken_by) VALUES (1, NULL)");
        }
    }

    private static String seat() throws Exception {
        try (Connection c = admin(); Statement s = c.createStatement(); ResultSet r = s.executeQuery("SELECT taken_by FROM seat WHERE id = 1")) {
            r.next();
            return r.getString(1);
        }
    }

    /** Captures frames and records terminal publication in the journal with its observed proxy state. */
    static final class Output extends Fakes.Output {
        Peer peer;

        @Override
        public boolean enqueue(byte[] payload) {
            boolean ok = super.enqueue(payload);
            JsonNode frame = frames.getLast();
            if (frame.get("type").stringValue().equals("terminal")) {
                Peer.Invocation invocation = peer.invocations.get(frame.get("body").get("invocation").stringValue());
                Journal.add("terminal proxy=" + invocation.proxy + " lease=" + invocation.lease);
            }
            synchronized (this) {
                notifyAll();
            }
            return ok;
        }

        synchronized JsonNode await(Predicate<JsonNode> match) throws InterruptedException {
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
            while (true) {
                for (JsonNode frame : frames) {
                    if (match.test(frame)) {
                        return frame;
                    }
                }
                long remaining = TimeUnit.NANOSECONDS.toMillis(deadline - System.nanoTime());
                if (remaining <= 0) {
                    throw new AssertionError("frame not observed: " + frames);
                }
                wait(remaining);
            }
        }
    }

    static final class Exit extends Fakes.Exit {
        @Override
        public synchronized void halt(int code) {
            super.halt(code);
            notifyAll();
        }

        synchronized int await() throws InterruptedException {
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
            while (status == null) {
                long remaining = TimeUnit.NANOSECONDS.toMillis(deadline - System.nanoTime());
                if (remaining <= 0) {
                    throw new AssertionError("peer did not exit");
                }
                wait(remaining);
            }
            return status;
        }
    }

    /** Records the command proxy's return or throw outside the SDK's own milestone state. */
    record ProxyJournal(SpringHost delegate) implements Seams.Host {
        @Override
        public void initialize(Seams.Start start) {
            delegate.initialize(start);
        }

        @Override
        public Map<String, Set<String>> validateRegistration(List<String> commands, List<String> points) {
            return delegate.validateRegistration(commands, points);
        }

        @Override
        public void probeDatabase() throws Exception {
            delegate.probeDatabase();
        }

        @Override
        public void cancelStartup() {
            delegate.cancelStartup();
        }

        @Override
        public void execute(CommandContext context) throws Throwable {
            try {
                delegate.execute(context);
            } catch (Throwable t) {
                Journal.add("proxy-exit thrown");
                throw t;
            }
            Journal.add("proxy-exit returned");
        }

        @Override
        public void close() {
            delegate.close();
        }
    }

    /** One real child session in-process: real threads, clock, Spring context, pool and driver. */
    static final class Session {
        final Output output = new Output();
        final Exit exit = new Exit();
        final SpringHost host;
        volatile FaultyDataSource faults;
        final Peer peer;
        int seq = 1;

        Session() throws Exception {
            this(30000, new String[0]);
        }

        /** Fatal sessions use a short cleanup bound: unproven cleanup must end in forced exit. */
        Session(int cancelMillis) throws Exception {
            this(cancelMillis, new String[0]);
        }

        Session(int cancelMillis, String[] springArgs) throws Exception {
            host = new SpringHost(FixtureApplication.class, springArgs, ds -> faults = new FaultyDataSource(ds));
            peer = new Peer(new ProxyJournal(host), new SystemSeams.MonotonicClock(), exit, output, SystemSeams.THREADS,
                    Seams.Activity.NONE);
            host.bind(peer);
            output.peer = peer;
            peer.receive(Scripted.frame("start", seq, Map.of("variant", "fixed", "params", Map.of(),
                    "commands", List.of("assign", "navigate", "fail_body", "rollback_only", "after_commit_failure",
                            "after_commit_jdbc", "after_completion_jdbc",
                            "duplicate_key", "caught_duplicate", "manual_commit", "sql_commit", "implicit_commit",
                            "package_private", "multi_statement", "session_select"),
                    "points", List.of("after_read", "before_write"), "capacity", 2,
                    "database", Map.of("driver", "mysql", "host", MYSQL.getHost(), "port", MYSQL.getMappedPort(3306),
                            "name", "weavegate", "username", "synthetic", "password", "synthetic-only"),
                    "startup_ms", 120000, "cancel_ms", cancelMillis)));
            output.await(f -> f.get("type").stringValue().equals("ready"));
        }

        JsonNode invoke(String invocation, String worker, String command) throws Exception {
            Journal.EVENTS.clear();
            peer.receive(Scripted.invoke(++seq, invocation, worker, command));
            return output.await(f -> f.get("type").stringValue().equals("accepted")
                    && f.get("body").get("invocation").stringValue().equals(invocation));
        }

        JsonNode arrival(String invocation) throws Exception {
            return output.await(f -> f.get("type").stringValue().equals("arrive")
                    && f.get("body").get("invocation").stringValue().equals(invocation));
        }

        JsonNode terminal(String invocation) throws Exception {
            return output.await(f -> f.get("type").stringValue().equals("terminal")
                    && f.get("body").get("invocation").stringValue().equals(invocation)).get("body");
        }

        JsonNode fatal() throws Exception {
            return output.await(f -> f.get("type").stringValue().equals("fatal")).get("body");
        }

        void stop() throws Exception {
            peer.receive(Scripted.stop(++seq, 60000));
            output.await(f -> f.get("type").stringValue().equals("stopped"));
            assertThat(exit.await()).isZero();
            assertThat(peer.openLeases).isZero();
        }
    }

    private static String id(int n) {
        return String.format("%032x", n);
    }

    @TestFactory
    Stream<DynamicTest> jdkProxyExecutesInsideTheRealTransaction() {
        return RequirementsTest.repeated(() -> {
            resetSeat();
            Session s = new Session(30000, new String[] {"--spring.aop.proxy-target-class=false"});
            org.springframework.context.ConfigurableApplicationContext context =
                    (org.springframework.context.ConfigurableApplicationContext)
                            org.springframework.test.util.ReflectionTestUtils.getField(s.host, "context");
            assertThat(org.springframework.aop.support.AopUtils.isJdkDynamicProxy(
                    context.getBean(SeatCommandApi.class))).isTrue();
            s.invoke(id(17), "w1", "assign");
            JsonNode arrive = s.arrival(id(17)).get("body");
            s.peer.receive(Scripted.release(++s.seq, id(17), "w1", arrive.get("arrival").stringValue(), "after_read"));
            assertThat(s.terminal(id(17)).toString()).contains("\"transaction\":\"committed\"",
                    "\"connection\":\"returned\"", "\"error\":null");
            assertThat(Journal.EVENTS).containsExactly("body-end", "driver-commit", "driver-close",
                    "proxy-exit returned", "terminal proxy=EXITED lease=RETURNED");
            assertThat(seat()).isEqualTo("w1");
            s.stop();
            EvidenceListener.check("requirement/java-jdk-command", "observe/evidence",
                    HANDLER + "jdkProxyExecutesInsideTheRealTransaction");
        });
    }

    @TestFactory
    Stream<DynamicTest> rejectedSessionSqlLeavesPooledConnectionUnchanged() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("session-state").quiet();
            h.peer.phase = Peer.Phase.STARTING;
            h.peer.probeThread = Thread.currentThread();
            try (HikariDataSource pool = new HikariDataSource()) {
                pool.setJdbcUrl(MYSQL.getJdbcUrl());
                pool.setUsername("synthetic");
                pool.setPassword("synthetic-only");
                pool.setMaximumPoolSize(1);
                try (Connection baseline = pool.getConnection(); Statement statement = baseline.createStatement()) {
                    statement.execute("SET @weavegate_fixture = 7");
                }
                try (Connection c = new TrackingDataSource(pool, h.peer).getConnection();
                     Statement statement = c.createStatement()) {
                    org.assertj.core.api.Assertions.assertThatThrownBy(
                            () -> statement.executeQuery("SELECT @weavegate_fixture"))
                            .isInstanceOf(java.sql.SQLFeatureNotSupportedException.class);
                    org.assertj.core.api.Assertions.assertThatThrownBy(
                            () -> statement.execute("SELECT 1 INTO @weavegate_fixture"))
                            .isInstanceOf(java.sql.SQLFeatureNotSupportedException.class);
                }
                try (Connection afterLease = pool.getConnection(); Statement statement = afterLease.createStatement();
                     ResultSet after = statement.executeQuery("SELECT @weavegate_fixture")) {
                        assertThat(after.next()).isTrue();
                        assertThat(after.getInt(1)).isEqualTo(7);
                }
                assertThat(h.peer.openLeases).isZero();
            }
        });
    }

    @TestFactory
    Stream<DynamicTest> postCommitCallbacksCannotPerformJdbcWork() {
        return RequirementsTest.repeated(() -> {
            for (String command : List.of("after_commit_jdbc", "after_completion_jdbc")) {
                resetSeat();
                Session s = new Session(1000);
                s.invoke(id(15), "w1", command);
                JsonNode fatal = s.fatal();
                assertThat(fatal.get("kind").stringValue()).isEqualTo("transaction");
                assertThat(fatal.get("message").stringValue()).isEqualTo("JDBC operation outside active transaction");
                assertThat(s.output.frames.stream().filter(f -> f.get("type").stringValue().equals("terminal"))).isEmpty();
                assertThat(s.exit.await()).isEqualTo(1);
                assertThat(seat()).isEqualTo("w1");
            }
        });
    }

    @TestFactory
    Stream<DynamicTest> applicationFailurePrecedesCancellationDuringClose() {
        return RequirementsTest.repeated(() -> {
            Session s = new Session();
            int n = 100;
            for (String command : List.of("fail_body", "after_commit_failure")) {
                resetSeat();
                String invocation = id(n++);
                s.faults.closeEntered = new java.util.concurrent.CountDownLatch(1);
                s.faults.closeAllowed = new java.util.concurrent.CountDownLatch(1);
                try {
                    s.invoke(invocation, "w1", command);
                    assertThat(s.faults.closeEntered.await(30, TimeUnit.SECONDS)).isTrue();
                    s.peer.receive(Scripted.cancel(++s.seq, invocation, "w1", "context"));
                } finally {
                    s.faults.closeAllowed.countDown();
                }
                JsonNode terminal = s.terminal(invocation);
                assertThat(terminal.get("transaction").stringValue()).isEqualTo(
                        command.equals("fail_body") ? "rolled_back" : "committed");
                assertThat(terminal.get("error").get("kind").stringValue()).isEqualTo("application");
                assertThat(terminal.get("error").get("message").stringValue()).contains("synthetic");
            }
            s.stop();
            EvidenceListener.marker("EXTERNAL_SUT_JAVA_FAILURE_ORDER_RESULT body=before_cleanup after_commit=before_cleanup cancellation=later");
        });
    }

    @TestFactory
    Stream<DynamicTest> beginFailurePrecedesCancellationDuringClose() {
        return RequirementsTest.repeated(() -> {
            Session s = new Session();
            String invocation = id(102);
            s.faults.fault = FaultyDataSource.Fault.BEGIN;
            s.faults.closeEntered = new java.util.concurrent.CountDownLatch(1);
            s.faults.closeAllowed = new java.util.concurrent.CountDownLatch(1);
            try {
                s.invoke(invocation, "w1", "assign");
                assertThat(s.faults.closeEntered.await(30, TimeUnit.SECONDS)).isTrue();
                s.peer.receive(Scripted.cancel(++s.seq, invocation, "w1", "context"));
            } finally {
                s.faults.closeAllowed.countDown();
            }
            JsonNode terminal = s.terminal(invocation);
            assertThat(terminal.toString()).contains("\"transaction\":\"not_started\"",
                    "\"connection\":\"returned\"", "\"kind\":\"application\"", "database operation failed")
                    .doesNotContain("cancelled by context");
            s.faults.fault = FaultyDataSource.Fault.NONE;
            s.stop();
        });
    }

    @TestFactory
    Stream<DynamicTest> springTransactionBoundaries() {
        return RequirementsTest.repeated(() -> {
            // Success, rollback, rollback-only, after-commit error, MySQL error and cancellation share one session.
            Session s = new Session();

            resetSeat();
            s.invoke(id(1), "w1", "assign");
            JsonNode arrive = s.arrival(id(1)).get("body");
            s.peer.receive(Scripted.release(++s.seq, id(1), "w1", arrive.get("arrival").stringValue(), "after_read"));
            JsonNode committed = s.terminal(id(1));
            assertThat(committed.toString()).isEqualTo("{\"invocation\":\"" + id(1)
                    + "\",\"worker\":\"w1\",\"transaction\":\"committed\",\"connection\":\"returned\",\"error\":null}");
            assertThat(Journal.EVENTS).containsExactly("body-end", "driver-commit", "driver-close", "proxy-exit returned",
                    "terminal proxy=EXITED lease=RETURNED");
            assertThat(seat()).isEqualTo("w1");

            resetSeat();
            s.invoke(id(2), "w1", "fail_body");
            assertThat(s.terminal(id(2)).toString()).contains("\"transaction\":\"rolled_back\"", "\"connection\":\"returned\"",
                    "\"kind\":\"application\"", "synthetic body failure");
            assertThat(Journal.EVENTS).containsExactly("body-end", "driver-rollback", "driver-close", "proxy-exit thrown",
                    "terminal proxy=EXITED lease=RETURNED");
            assertThat(seat()).isNull();

            s.invoke(id(3), "w1", "rollback_only");
            assertThat(s.terminal(id(3)).toString()).contains("\"transaction\":\"rolled_back\"", "transaction rolled back");
            assertThat(Journal.EVENTS).containsExactly("body-end", "driver-rollback", "driver-close", "proxy-exit returned",
                    "terminal proxy=EXITED lease=RETURNED");
            assertThat(seat()).isNull();

            s.invoke(id(4), "w1", "after_commit_failure");
            assertThat(s.terminal(id(4)).toString()).contains("\"transaction\":\"committed\"", "\"kind\":\"application\"",
                    "synthetic after-commit failure");
            assertThat(Journal.EVENTS).containsExactly("body-end", "driver-commit", "driver-close", "proxy-exit thrown",
                    "terminal proxy=EXITED lease=RETURNED");
            assertThat(seat()).isEqualTo("w1");

            s.invoke(id(5), "w1", "duplicate_key");
            assertThat(s.terminal(id(5)).toString()).contains("\"transaction\":\"rolled_back\"", "\"kind\":\"mysql\"",
                    "\"mysql_code\":1062", "\"sql_state\":\"23000\"", "MySQL operation failed")
                    .doesNotContain("Duplicate entry", "duplicate_key", "INSERT INTO");

            s.invoke(id(10), "w1", "caught_duplicate");
            assertThat(s.terminal(id(10)).toString()).contains("\"transaction\":\"committed\"", "\"error\":null");
            assertThat(Journal.EVENTS).containsExactly("body-end", "driver-commit", "driver-close",
                    "proxy-exit returned", "terminal proxy=EXITED lease=RETURNED");

            resetSeat();
            s.invoke(id(11), "w1", "manual_commit");
            assertThat(s.terminal(id(11)).toString()).contains("\"transaction\":\"rolled_back\"",
                    "\"kind\":\"application\"", "application-managed transaction control is unsupported");
            assertThat(seat()).isNull();

            resetSeat();
            s.invoke(id(12), "w1", "sql_commit");
            JsonNode sqlControl = s.terminal(id(12));
            assertThat(seat()).isNull();
            assertThat(sqlControl.toString()).contains("\"transaction\":\"rolled_back\"",
                    "\"kind\":\"application\"", "application-managed transaction control is unsupported");

            resetSeat();
            s.invoke(id(13), "w1", "package_private");
            assertThat(s.terminal(id(13)).toString()).contains("\"transaction\":\"committed\"", "\"error\":null");
            assertThat(seat()).isEqualTo("w1");

            // Begin failure: the lease was acquired and returned, but no transaction started.
            s.faults.fault = FaultyDataSource.Fault.BEGIN;
            s.invoke(id(6), "w1", "assign");
            assertThat(s.terminal(id(6)).toString()).contains("\"transaction\":\"not_started\"", "\"connection\":\"returned\"",
                    "\"kind\":\"application\"");
            assertThat(Journal.EVENTS).containsExactly("driver-close", "proxy-exit thrown", "terminal proxy=EXITED lease=RETURNED");
            s.faults.fault = FaultyDataSource.Fault.NONE;

            // Cancellation at a gate while w2 is blocked in JDBC on the same row lock.
            resetSeat();
            s.invoke(id(7), "w1", "assign");
            s.arrival(id(7));
            s.faults.lockingRead = new java.util.concurrent.CountDownLatch(1);
            s.faults.statementCancel = new java.util.concurrent.CountDownLatch(1);
            s.invoke(id(8), "w2", "navigate");
            assertThat(s.faults.lockingRead.await(60, TimeUnit.SECONDS)).isTrue();
            s.peer.receive(Scripted.cancel(++s.seq, id(8), "w2", "context"));
            assertThat(s.faults.statementCancel.await(60, TimeUnit.SECONDS)).isTrue();
            // Whether the driver's cancel request reaches the server before the lock wait is
            // not decidable here; releasing w1 by cancellation keeps both outcomes bounded.
            s.peer.receive(Scripted.cancel(++s.seq, id(7), "w1", "context"));
            assertThat(s.terminal(id(8)).toString()).contains("\"transaction\":\"rolled_back\"", "\"kind\":\"cancelled\"",
                    "cancelled by context");
            assertThat(s.peer.invocations.get(id(8)).jdbcCancelRequests).isEqualTo(1);
            assertThat(s.terminal(id(7)).toString()).contains("\"transaction\":\"rolled_back\"", "cancelled by context");
            assertThat(seat()).isNull();
            s.stop();

            assertFatal(FaultyDataSource.Fault.COMMIT, "transaction", "transaction outcome unknown", "assign", "thrown");
            assertFatal(FaultyDataSource.Fault.ROLLBACK, "transaction", "transaction outcome unknown", "fail_body", "thrown");
            assertFatal(FaultyDataSource.Fault.CLOSE, "cleanup", "lease return failed", "assign", "returned");
            assertUnsupportedSqlFatal();
            assertSqlAdmissionFatal("multi_statement");
            assertSqlAdmissionFatal("session_select");
            EvidenceListener.check("requirement/spring-transactions", "observe/evidence", HANDLER + "springTransactionBoundaries");
        });
    }

    private static void assertUnsupportedSqlFatal() throws Exception {
        resetSeat();
        Session s = new Session(1000);
        s.invoke(id(14), "w1", "implicit_commit");
        JsonNode fatal = s.fatal();
        assertThat(fatal.get("kind").stringValue()).isEqualTo("transaction");
        assertThat(fatal.get("message").stringValue()).isEqualTo("nontransactional SQL is unsupported");
        assertThat(s.output.frames.stream().filter(f -> f.get("type").stringValue().equals("terminal"))).isEmpty();
        assertThat(s.exit.await()).isEqualTo(1);
        assertThat(seat()).isNull();
    }

    private static void assertSqlAdmissionFatal(String command) throws Exception {
        resetSeat();
        Session s = new Session(1000);
        s.invoke(id(16), "w1", command);
        JsonNode fatal = s.fatal();
        assertThat(fatal.get("kind").stringValue()).isEqualTo("transaction");
        assertThat(fatal.get("message").stringValue()).isEqualTo("SQL outside the supported subset");
        assertThat(s.output.frames.stream().filter(f -> f.get("type").stringValue().equals("terminal"))).isEmpty();
        assertThat(s.exit.await()).isEqualTo(1);
        assertThat(seat()).isNull();
    }

    private static void assertFatal(FaultyDataSource.Fault fault, String kind, String message, String command,
                                    String proxyExit) throws Exception {
        resetSeat();
        Session s = new Session(1000);
        s.faults.fault = fault;
        s.invoke(id(9), "w1", command);
        if (command.equals("assign")) {
            JsonNode arrive = s.arrival(id(9)).get("body");
            s.peer.receive(Scripted.release(++s.seq, id(9), "w1", arrive.get("arrival").stringValue(), "after_read"));
        }
        JsonNode fatal = s.fatal();
        assertThat(fatal.get("kind").stringValue()).isEqualTo(kind);
        assertThat(fatal.get("message").stringValue()).isEqualTo(message);
        assertThat(s.output.frames.stream().filter(f -> f.get("type").stringValue().equals("terminal"))).isEmpty();
        assertThat(s.exit.await()).isEqualTo(1);
        assertThat(s.output.frames.stream().filter(f -> f.get("type").stringValue().equals("stopped"))).isEmpty();
        // Spring suppresses close failures and reports commit/rollback failures from the proxy.
        assertThat(Journal.EVENTS).contains("proxy-exit " + proxyExit);
    }
}
