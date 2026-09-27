package io.github.weavegate.sdk;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Savepoint;
import java.sql.Statement;
import java.util.concurrent.Executor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import java.util.stream.Stream;
import javax.sql.DataSource;

import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.TestFactory;
import org.springframework.test.util.ReflectionTestUtils;

class TrackingHandlesTest {
    interface VendorConnection extends Connection { }
    interface VendorStatement extends Statement { }

    @TestFactory
    Stream<DynamicTest> sqlAdmissionRejectsEscapingEffectsBeforeDelegation() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            h.peer.phase = Peer.Phase.STARTING;
            h.peer.probeThread = Thread.currentThread();
            DataSource source = mock(DataSource.class);
            Connection raw = mock(Connection.class);
            Statement statement = mock(Statement.class);
            when(source.getConnection()).thenReturn(raw);
            when(raw.createStatement()).thenReturn(statement);
            try (Connection tracked = new TrackingDataSource(source, h.peer).getConnection();
                 Statement wrapped = tracked.createStatement()) {
                for (String sql : java.util.List.of(
                        "SELECT 1; COMMIT", "SELECT 'safe; literal'; COMMIT",
                        "SELECT 1--x; COMMIT",
                        "/* comment */ SHOW TABLES", "/*!80000 COMMIT */ SELECT 1",
                        "SELECT 1 INTO @fixture", "SELECT @fixture := 1",
                        "SELECT @fixture", "SELECT @@session.autocommit", "SELECT @'fixture'",
                        "SELECT IF(1, LAST_INSERT_ID(2), 0)",
                        "UPDATE seat SET taken_by = 'x'; COMMIT")) {
                    assertThatThrownBy(() -> wrapped.execute(sql)).isInstanceOf(SQLException.class);
                    assertThatThrownBy(() -> wrapped.addBatch(sql)).isInstanceOf(SQLException.class);
                    assertThatThrownBy(() -> tracked.prepareStatement(sql)).isInstanceOf(SQLException.class);
                    verify(statement, org.mockito.Mockito.never()).execute(sql);
                    verify(statement, org.mockito.Mockito.never()).addBatch(sql);
                    verify(raw, org.mockito.Mockito.never()).prepareStatement(sql);
                }
                wrapped.execute("SELECT 'safe; literal'");
                verify(statement).execute("SELECT 'safe; literal'");
                wrapped.execute("SELECT '@fixture', `@column`");
                verify(statement).execute("SELECT '@fixture', `@column`");
            }
        });
    }

    @TestFactory
    Stream<DynamicTest> resultSetMutationsCannotBypassSqlAdmission() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            h.peer.phase = Peer.Phase.STARTING;
            h.peer.probeThread = Thread.currentThread();
            DataSource source = mock(DataSource.class);
            Connection raw = mock(Connection.class);
            Statement statement = mock(Statement.class);
            ResultSet rows = mock(ResultSet.class);
            when(source.getConnection()).thenReturn(raw);
            when(raw.createStatement(ResultSet.TYPE_SCROLL_INSENSITIVE, ResultSet.CONCUR_UPDATABLE))
                    .thenReturn(statement);
            when(statement.executeQuery("SELECT id FROM seat")).thenReturn(rows);
            try (Connection tracked = new TrackingDataSource(source, h.peer).getConnection();
                 Statement wrapped = tracked.createStatement(ResultSet.TYPE_SCROLL_INSENSITIVE,
                         ResultSet.CONCUR_UPDATABLE);
                 ResultSet result = wrapped.executeQuery("SELECT id FROM seat")) {
                assertThatThrownBy(() -> result.updateInt(1, 2)).isInstanceOf(SQLException.class);
                assertThatThrownBy(result::updateRow).isInstanceOf(SQLException.class);
                assertThatThrownBy(result::insertRow).isInstanceOf(SQLException.class);
                assertThatThrownBy(result::deleteRow).isInstanceOf(SQLException.class);
                verify(rows, org.mockito.Mockito.never()).updateInt(1, 2);
                verify(rows, org.mockito.Mockito.never()).updateRow();
                verify(rows, org.mockito.Mockito.never()).insertRow();
                verify(rows, org.mockito.Mockito.never()).deleteRow();
            }
        });
    }

    @TestFactory
    Stream<DynamicTest> jdbcBatchesAreRejectedBeforeDelegation() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            h.peer.phase = Peer.Phase.STARTING;
            h.peer.probeThread = Thread.currentThread();
            DataSource source = mock(DataSource.class);
            Connection raw = mock(Connection.class);
            Statement statement = mock(Statement.class);
            PreparedStatement prepared = mock(PreparedStatement.class);
            when(source.getConnection()).thenReturn(raw);
            when(raw.createStatement()).thenReturn(statement);
            when(raw.prepareStatement("UPDATE seat SET taken_by = 'fixture'")).thenReturn(prepared);
            try (Connection tracked = new TrackingDataSource(source, h.peer).getConnection();
                 Statement wrapped = tracked.createStatement();
                 PreparedStatement wrappedPrepared = tracked.prepareStatement(
                         "UPDATE seat SET taken_by = 'fixture'")) {
                assertThatThrownBy(() -> wrapped.addBatch("UPDATE seat SET taken_by = 'fixture'"))
                        .isInstanceOf(SQLException.class);
                assertThatThrownBy(wrapped::executeBatch).isInstanceOf(SQLException.class);
                assertThatThrownBy(wrapped::executeLargeBatch).isInstanceOf(SQLException.class);
                assertThatThrownBy(wrappedPrepared::addBatch).isInstanceOf(SQLException.class);
                assertThatThrownBy(wrappedPrepared::executeBatch).isInstanceOf(SQLException.class);
                assertThatThrownBy(wrappedPrepared::executeLargeBatch).isInstanceOf(SQLException.class);
                verify(statement, org.mockito.Mockito.never()).addBatch("UPDATE seat SET taken_by = 'fixture'");
                verify(statement, org.mockito.Mockito.never()).executeBatch();
                verify(statement, org.mockito.Mockito.never()).executeLargeBatch();
                verify(prepared, org.mockito.Mockito.never()).addBatch();
                verify(prepared, org.mockito.Mockito.never()).executeBatch();
                verify(prepared, org.mockito.Mockito.never()).executeLargeBatch();
                wrapped.execute("SELECT 1");
                verify(statement).execute("SELECT 1");
            }
        });
    }

    @TestFactory
    Stream<DynamicTest> sessionAndResourceEntrypointsCannotBypassTracking() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            h.peer.phase = Peer.Phase.STARTING;
            h.peer.probeThread = Thread.currentThread();
            DataSource source = mock(DataSource.class);
            Connection raw = mock(Connection.class);
            Statement statement = mock(Statement.class);
            ResultSet rows = mock(ResultSet.class);
            when(source.getConnection()).thenReturn(raw);
            when(raw.createStatement()).thenReturn(statement);
            when(statement.executeQuery("SELECT 1")).thenReturn(rows);
            try (Connection tracked = new TrackingDataSource(source, h.peer).getConnection();
                 Statement wrapped = tracked.createStatement();
                 ResultSet result = wrapped.executeQuery("SELECT 1")) {
                assertThatThrownBy(() -> tracked.abort(Runnable::run)).isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> tracked.setCatalog("other")).isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> tracked.setSchema("other")).isInstanceOf(SQLException.class);
                assertThatThrownBy(tracked::createBlob).isInstanceOf(SQLException.class);
                assertThatThrownBy(wrapped::closeOnCompletion).isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> result.getBlob(1)).isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> result.getObject(1)).isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> result.getBinaryStream(1)).isInstanceOf(SQLException.class);
                verify(raw, org.mockito.Mockito.never()).abort(org.mockito.Mockito.any());
                verify(raw, org.mockito.Mockito.never()).setCatalog(org.mockito.Mockito.anyString());
                verify(raw, org.mockito.Mockito.never()).createBlob();
                verify(statement, org.mockito.Mockito.never()).closeOnCompletion();
                verify(rows, org.mockito.Mockito.never()).getBlob(1);
                verify(rows, org.mockito.Mockito.never()).getObject(1);
            }
        });
    }

    @TestFactory
    Stream<DynamicTest> namedLocksCannotEscapeTransactionLifetime() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            h.peer.phase = Peer.Phase.STARTING;
            h.peer.probeThread = Thread.currentThread();
            DataSource source = mock(DataSource.class);
            Connection raw = mock(Connection.class);
            Statement statement = mock(Statement.class);
            when(source.getConnection()).thenReturn(raw);
            when(raw.createStatement()).thenReturn(statement);
            try (Connection tracked = new TrackingDataSource(source, h.peer).getConnection();
                 Statement wrapped = tracked.createStatement()) {
                for (String sql : java.util.List.of("SELECT GET_LOCK('fixture', 1)",
                        "SELECT release_lock('fixture')", "SELECT RELEASE_ALL_LOCKS()",
                        "SELECT GET_LOCK /* comment */ ('fixture', 0)",
                        "SELECT /*!80000 GET_LOCK('fixture', 0) */")) {
                    assertThatThrownBy(() -> wrapped.execute(sql)).isInstanceOf(SQLException.class);
                    assertThatThrownBy(() -> wrapped.addBatch(sql)).isInstanceOf(SQLException.class);
                    assertThatThrownBy(() -> tracked.prepareStatement(sql)).isInstanceOf(SQLException.class);
                    verify(statement, org.mockito.Mockito.never()).execute(sql);
                    verify(statement, org.mockito.Mockito.never()).addBatch(sql);
                    verify(raw, org.mockito.Mockito.never()).prepareStatement(sql);
                }
                wrapped.execute("SELECT 'GET_LOCK( is only text'");
                verify(statement).execute("SELECT 'GET_LOCK( is only text'");
            }
        });
    }

    @TestFactory
    Stream<DynamicTest> dataSourceLoginTimeoutCannotBeChangedToNonzero() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            DataSource source = mock(DataSource.class);
            TrackingDataSource tracked = new TrackingDataSource(source, h.peer);
            assertThatThrownBy(() -> tracked.setLoginTimeout(1)).isInstanceOf(SQLException.class);
            assertThatThrownBy(() -> tracked.setLoginTimeout(-1)).isInstanceOf(SQLException.class);
            verify(source, org.mockito.Mockito.never()).setLoginTimeout(1);
            verify(source, org.mockito.Mockito.never()).setLoginTimeout(-1);
            tracked.setLoginTimeout(0);
            verify(source).setLoginTimeout(0);
        });
    }

    @TestFactory
    Stream<DynamicTest> rejectedOutsideLeaseIsClosedBeforeItCanExecute() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            h.peer.phase = Peer.Phase.READY;
            h.peer.startupDone = true;
            DataSource source = mock(DataSource.class);
            Connection raw = mock(Connection.class);
            when(source.getConnection()).thenReturn(raw);
            assertThatThrownBy(() -> new TrackingDataSource(source, h.peer).getConnection())
                    .isInstanceOf(SQLException.class);
            verify(raw).close();
            assertThat(h.peer.openLeases).isZero();
            assertThat(h.peer.fatalKind).isEqualTo("protocol");
        });
    }

    @TestFactory
    Stream<DynamicTest> startupCallbackCannotExecuteSqlOutsideProbe() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            h.peer.phase = Peer.Phase.STARTING;
            DataSource source = mock(DataSource.class);
            Connection raw = mock(Connection.class);
            Statement statement = mock(Statement.class);
            when(source.getConnection()).thenReturn(raw);
            when(raw.createStatement()).thenReturn(statement);
            assertThatThrownBy(() -> {
                try (Connection tracked = new TrackingDataSource(source, h.peer).getConnection();
                     Statement sql = tracked.createStatement()) {
                    sql.execute("UPDATE seat SET taken_by = 'startup'");
                }
            }).isInstanceOf(SQLException.class);
            verify(statement, org.mockito.Mockito.never()).execute("UPDATE seat SET taken_by = 'startup'");
            verify(raw).close();
            assertThat(h.peer.openLeases).isZero();
            assertThat(h.peer.fatalKind).isEqualTo("protocol");
        });
    }

    @TestFactory
    Stream<DynamicTest> applicationJdbcRequiresAnActiveTransaction() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            DataSource source = mock(DataSource.class);
            Connection raw = mock(Connection.class);
            when(source.getConnection()).thenReturn(raw);
            Peer.Invocation invocation = h.peer.new Invocation(Scripted.I1, "w1", "assign");
            ThreadLocal<Peer.Invocation> current = current();
            invocation.thread = Thread.currentThread();
            invocation.proxy = Peer.Proxy.INSIDE;
            current.set(invocation);
            try {
                assertThatThrownBy(() -> new TrackingDataSource(source, h.peer).getConnection())
                        .isInstanceOf(WeavegateCancelledException.class);
                verify(source, org.mockito.Mockito.never()).getConnection();
                assertThat(h.peer.fatalKind).isEqualTo("transaction");
            } finally {
                current.remove();
            }
        });
    }

    @TestFactory
    Stream<DynamicTest> metadataCannotRunOutsideCancellationTracking() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            DataSource source = mock(DataSource.class);
            Connection raw = mock(Connection.class);
            when(source.getConnection()).thenReturn(raw);
            Peer.Invocation invocation = h.peer.new Invocation(Scripted.I1, "w1", "assign");
            ThreadLocal<Peer.Invocation> current = current();
            invocation.thread = Thread.currentThread();
            invocation.proxy = Peer.Proxy.INSIDE;
            invocation.transaction = Peer.Transaction.ACTIVE;
            current.set(invocation);
            try {
                Connection tracked = new TrackingDataSource(source, h.peer).getConnection();
                assertThatThrownBy(tracked::getMetaData).isInstanceOf(SQLException.class);
                verify(raw, org.mockito.Mockito.never()).getMetaData();
                tracked.close();
            } finally {
                current.remove();
            }
        });
    }

    @TestFactory
    Stream<DynamicTest> startupCannotRetainAnUntrackedMetadataHandle() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            h.peer.phase = Peer.Phase.STARTING;
            h.peer.probeThread = Thread.currentThread();
            DataSource source = mock(DataSource.class);
            Connection raw = mock(Connection.class);
            when(source.getConnection()).thenReturn(raw);
            Connection tracked = new TrackingDataSource(source, h.peer).getConnection();
            assertThatThrownBy(tracked::getMetaData).isInstanceOf(SQLException.class);
            verify(raw, org.mockito.Mockito.never()).getMetaData();
            tracked.close();
            assertThat(h.peer.openLeases).isZero();
        });
    }

    @TestFactory
    Stream<DynamicTest> jdbcNavigationCannotEscapeTracking() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            h.peer.phase = Peer.Phase.STARTING;
            h.peer.probeThread = Thread.currentThread();
            DataSource source = mock(DataSource.class);
            Connection raw = mock(VendorConnection.class);
            Statement statement = mock(VendorStatement.class);
            ResultSet rows = mock(ResultSet.class);
            when(source.getConnection()).thenReturn(raw);
            when(raw.createStatement()).thenReturn(statement);
            when(raw.unwrap(Connection.class)).thenReturn(raw);
            when(raw.unwrap(VendorConnection.class)).thenReturn((VendorConnection) raw);
            when(statement.getConnection()).thenReturn(raw);
            when(statement.unwrap(Statement.class)).thenReturn(statement);
            when(statement.unwrap(VendorStatement.class)).thenReturn((VendorStatement) statement);
            when(statement.executeQuery("SELECT 1")).thenReturn(rows);
            when(rows.getStatement()).thenReturn(statement);
            Connection tracked = new TrackingDataSource(source, h.peer).getConnection();
            assertThat(tracked.unwrap(Connection.class)).isSameAs(tracked);
            assertThat(tracked.isWrapperFor(VendorConnection.class)).isFalse();
            assertThatThrownBy(() -> tracked.unwrap(VendorConnection.class)).isInstanceOf(SQLException.class);
            Statement wrapped = tracked.createStatement();
            assertThat(wrapped.getConnection()).isSameAs(tracked);
            assertThat(wrapped.unwrap(Statement.class)).isSameAs(wrapped);
            assertThatThrownBy(() -> wrapped.unwrap(VendorStatement.class)).isInstanceOf(SQLException.class);
            ResultSet trackedRows = wrapped.executeQuery("SELECT 1");
            assertThat(trackedRows.getStatement()).isSameAs(wrapped);
            assertThat(trackedRows.equals(trackedRows)).isTrue();
            assertThat(trackedRows.equals(rows)).isFalse();
            assertThat(trackedRows.hashCode()).isEqualTo(System.identityHashCode(trackedRows));
            assertThat(trackedRows.toString()).isEqualTo("TrackedResultSet");
            assertThat(wrapped.equals(wrapped)).isTrue();
            assertThat(wrapped.equals(statement)).isFalse();
            assertThat(wrapped.hashCode()).isEqualTo(System.identityHashCode(wrapped));
            assertThat(wrapped.toString()).isEqualTo("TrackedStatement");
            tracked.close();
            assertThat(h.peer.openLeases).isZero();
        });
    }

    @TestFactory
    Stream<DynamicTest> caughtSqlExceptionsDoNotBecomeInvocationFailures() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            DataSource source = mock(DataSource.class);
            Connection raw = mock(Connection.class);
            Statement statement = mock(Statement.class);
            SQLException duplicate = new SQLException("Duplicate entry secret", "23000", 1062);
            when(source.getConnection()).thenReturn(raw);
            when(raw.createStatement()).thenReturn(statement);
            when(statement.execute("SELECT 1")).thenThrow(duplicate);
            Peer.Invocation invocation = h.peer.new Invocation(Scripted.I1, "w1", "assign");
            ThreadLocal<Peer.Invocation> current = current();
            invocation.thread = Thread.currentThread();
            invocation.proxy = Peer.Proxy.INSIDE;
            invocation.transaction = Peer.Transaction.ACTIVE;
            current.set(invocation);
            try {
                Connection tracked = new TrackingDataSource(source, h.peer).getConnection();
                try {
                    assertThatThrownBy(() -> tracked.createStatement().execute("SELECT 1")).isSameAs(duplicate);
                    assertThat(invocation.source).isNull();
                    h.peer.recordSource(invocation, duplicate);
                    assertThat(invocation.source).isNotSameAs(duplicate);
                    assertThat(invocation.source.getMessage()).isEqualTo("MySQL operation failed");
                } finally {
                    tracked.close();
                }
            } finally {
                current.remove();
            }
        });
    }

    @TestFactory
    Stream<DynamicTest> caughtDeadlockInvalidatesTheTransaction() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            DataSource source = mock(DataSource.class);
            Connection raw = mock(Connection.class);
            Statement statement = mock(Statement.class);
            SQLException deadlock = new SQLException("secret deadlock details", "40001", 1213);
            when(source.getConnection()).thenReturn(raw);
            when(raw.createStatement()).thenReturn(statement);
            when(statement.execute("SELECT 1")).thenThrow(deadlock);
            Peer.Invocation invocation = h.peer.new Invocation(Scripted.I1, "w1", "assign");
            ThreadLocal<Peer.Invocation> current = current();
            invocation.thread = Thread.currentThread();
            invocation.proxy = Peer.Proxy.INSIDE;
            invocation.transaction = Peer.Transaction.ACTIVE;
            current.set(invocation);
            try {
                Connection tracked = new TrackingDataSource(source, h.peer).getConnection();
                assertThatThrownBy(() -> tracked.createStatement().execute("SELECT 1")).isSameAs(deadlock);
                assertThat(invocation.transaction).isEqualTo(Peer.Transaction.UNKNOWN);
                assertThat(h.peer.fatalKind).isEqualTo("transaction");
                assertThat(invocation.source).isNull();
            } finally {
                current.remove();
            }
        });
    }

    @TestFactory
    Stream<DynamicTest> resultSetAndStatementCloseRemainCancellable() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            DataSource source = mock(DataSource.class);
            Connection raw = mock(Connection.class);
            Statement statement = mock(Statement.class);
            ResultSet rows = mock(ResultSet.class);
            AtomicReference<Boolean> registeredDuringNext = new AtomicReference<>();
            AtomicReference<Boolean> registeredDuringClose = new AtomicReference<>();
            AtomicReference<Boolean> registeredDuringStatementClose = new AtomicReference<>();
            AtomicReference<Boolean> registeredDuringMoreResults = new AtomicReference<>();
            AtomicReference<Boolean> registeredDuringMoreResultsMode = new AtomicReference<>();
            Peer.Invocation invocation = h.peer.new Invocation(Scripted.I1, "w1", "assign");
            when(source.getConnection()).thenReturn(raw);
            when(raw.createStatement()).thenReturn(statement);
            when(statement.executeQuery("SELECT 1")).thenReturn(rows);
            when(rows.next()).thenAnswer(ignored -> {
                synchronized (h.peer) {
                    registeredDuringNext.set(!invocation.statements.isEmpty());
                }
                return false;
            });
            doAnswer(ignored -> {
                synchronized (h.peer) {
                    registeredDuringClose.set(!invocation.statements.isEmpty());
                }
                return null;
            }).when(rows).close();
            doAnswer(ignored -> {
                synchronized (h.peer) {
                    registeredDuringStatementClose.set(!invocation.statements.isEmpty());
                }
                return null;
            }).when(statement).close();
            when(statement.getMoreResults()).thenAnswer(ignored -> {
                synchronized (h.peer) {
                    registeredDuringMoreResults.set(!invocation.statements.isEmpty());
                }
                return false;
            });
            when(statement.getMoreResults(Statement.CLOSE_CURRENT_RESULT)).thenAnswer(ignored -> {
                synchronized (h.peer) {
                    registeredDuringMoreResultsMode.set(!invocation.statements.isEmpty());
                }
                return false;
            });
            ThreadLocal<Peer.Invocation> current = current();
            invocation.thread = Thread.currentThread();
            invocation.proxy = Peer.Proxy.INSIDE;
            invocation.transaction = Peer.Transaction.ACTIVE;
            current.set(invocation);
            try {
                Connection tracked = new TrackingDataSource(source, h.peer).getConnection();
                Statement trackedStatement = tracked.createStatement();
                ResultSet trackedRows = trackedStatement.executeQuery("SELECT 1");
                assertThat(trackedRows.next()).isFalse();
                assertThat(registeredDuringNext.get()).isTrue();
                assertThat(trackedStatement.getMoreResults()).isFalse();
                assertThat(registeredDuringMoreResults.get()).isTrue();
                assertThat(trackedStatement.getMoreResults(Statement.CLOSE_CURRENT_RESULT)).isFalse();
                assertThat(registeredDuringMoreResultsMode.get()).isTrue();
                trackedRows.close();
                assertThat(registeredDuringClose.get()).isTrue();
                trackedStatement.close();
                assertThat(registeredDuringStatementClose.get()).isTrue();
                tracked.close();
            } finally {
                current.remove();
            }
        });
    }

    @TestFactory
    Stream<DynamicTest> applicationTransactionControlsAreRejected() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            h.peer.phase = Peer.Phase.STARTING;
            h.peer.probeThread = Thread.currentThread();
            DataSource source = mock(DataSource.class);
            Connection raw = mock(Connection.class);
            Savepoint savepoint = mock(Savepoint.class);
            when(source.getConnection()).thenReturn(raw);
            Connection tracked = new TrackingDataSource(source, h.peer).getConnection();
            assertThatThrownBy(() -> tracked.setAutoCommit(false)).isInstanceOf(SQLException.class);
            assertThatThrownBy(tracked::commit).isInstanceOf(SQLException.class);
            assertThatThrownBy(tracked::rollback).isInstanceOf(SQLException.class);
            assertThatThrownBy(tracked::setSavepoint).isInstanceOf(SQLException.class);
            assertThatThrownBy(() -> tracked.rollback(savepoint)).isInstanceOf(SQLException.class);
            assertThatThrownBy(() -> tracked.releaseSavepoint(savepoint)).isInstanceOf(SQLException.class);
            verify(raw, org.mockito.Mockito.never()).setAutoCommit(false);
            verify(raw, org.mockito.Mockito.never()).commit();
            verify(raw, org.mockito.Mockito.never()).rollback();
            verify(raw, org.mockito.Mockito.never()).setSavepoint();
            verify(raw, org.mockito.Mockito.never()).rollback(savepoint);
            verify(raw, org.mockito.Mockito.never()).releaseSavepoint(savepoint);
            tracked.close();
        });
    }

    @TestFactory
    Stream<DynamicTest> sqlTransactionControlsAreRejectedBeforeDelegation() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            DataSource source = mock(DataSource.class);
            Connection raw = mock(Connection.class);
            Statement statement = mock(Statement.class);
            PreparedStatement prepared = mock(PreparedStatement.class);
            when(source.getConnection()).thenReturn(raw);
            when(raw.createStatement()).thenReturn(statement);
            when(raw.prepareStatement("COMMIT")).thenReturn(prepared);
            Peer.Invocation invocation = h.peer.new Invocation(Scripted.I1, "w1", "assign");
            ThreadLocal<Peer.Invocation> current = current();
            invocation.thread = Thread.currentThread();
            invocation.proxy = Peer.Proxy.INSIDE;
            invocation.transaction = Peer.Transaction.ACTIVE;
            current.set(invocation);
            try {
                Connection tracked = new TrackingDataSource(source, h.peer).getConnection();
                Statement trackedStatement = tracked.createStatement();
                assertThatThrownBy(() -> trackedStatement.execute("COMMIT")).isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> trackedStatement.addBatch("/* fixture */ ROLLBACK"))
                        .isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> tracked.prepareStatement("COMMIT")).isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> trackedStatement.execute("SET SESSION autocommit = 1"))
                        .isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> trackedStatement.execute("SET /* fixture */ autocommit = 1"))
                        .isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> trackedStatement.execute("START /* fixture */ TRANSACTION"))
                        .isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> trackedStatement.execute("TRUNCATE TABLE seat"))
                        .isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> trackedStatement.execute("ALTER TABLE seat ADD COLUMN note TEXT"))
                        .isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> trackedStatement.execute("LOCK TABLES seat WRITE"))
                        .isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> trackedStatement.execute("SAVEPOINT fixture"))
                        .isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> trackedStatement.execute("PREPARE tx FROM 'COMMIT'"))
                        .isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> trackedStatement.execute("EXECUTE tx"))
                        .isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> trackedStatement.addBatch("DEALLOCATE PREPARE tx"))
                        .isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> trackedStatement.execute("DROP PREPARE tx"))
                        .isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> tracked.prepareCall("CALL mutate()"))
                        .isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> trackedStatement.execute("CALL mutate()"))
                        .isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> trackedStatement.execute("SET SESSION innodb_lock_wait_timeout = 1"))
                        .isInstanceOf(SQLException.class);
                assertThatThrownBy(() -> trackedStatement.execute("SELECT /*+ MAX_EXECUTION_TIME(1) */ 1"))
                        .isInstanceOf(SQLException.class);
                verify(statement, org.mockito.Mockito.never()).execute("COMMIT");
                verify(statement, org.mockito.Mockito.never()).addBatch("/* fixture */ ROLLBACK");
                verify(raw, org.mockito.Mockito.never()).prepareStatement("COMMIT");
                verify(statement, org.mockito.Mockito.never()).execute("SET SESSION autocommit = 1");
                verify(statement, org.mockito.Mockito.never()).execute("SET /* fixture */ autocommit = 1");
                verify(statement, org.mockito.Mockito.never()).execute("START /* fixture */ TRANSACTION");
                verify(statement, org.mockito.Mockito.never()).execute("TRUNCATE TABLE seat");
                verify(statement, org.mockito.Mockito.never()).execute("ALTER TABLE seat ADD COLUMN note TEXT");
                verify(statement, org.mockito.Mockito.never()).execute("LOCK TABLES seat WRITE");
                verify(statement, org.mockito.Mockito.never()).execute("SAVEPOINT fixture");
                verify(statement, org.mockito.Mockito.never()).execute("PREPARE tx FROM 'COMMIT'");
                verify(statement, org.mockito.Mockito.never()).execute("EXECUTE tx");
                verify(statement, org.mockito.Mockito.never()).addBatch("DEALLOCATE PREPARE tx");
                verify(statement, org.mockito.Mockito.never()).execute("DROP PREPARE tx");
                verify(raw, org.mockito.Mockito.never()).prepareCall("CALL mutate()");
                verify(statement, org.mockito.Mockito.never()).execute("CALL mutate()");
                verify(statement, org.mockito.Mockito.never()).execute("SET SESSION innodb_lock_wait_timeout = 1");
                verify(statement, org.mockito.Mockito.never()).execute("SELECT /*+ MAX_EXECUTION_TIME(1) */ 1");
                tracked.close();
            } finally {
                current.remove();
            }
        });
    }

    @TestFactory
    Stream<DynamicTest> jdbcAfterCommitIsRejectedBeforeDelegation() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            DataSource source = mock(DataSource.class);
            Connection raw = mock(Connection.class);
            Statement statement = mock(Statement.class);
            when(source.getConnection()).thenReturn(raw);
            when(raw.createStatement()).thenReturn(statement);
            Peer.Invocation invocation = h.peer.new Invocation(Scripted.I1, "w1", "assign");
            ThreadLocal<Peer.Invocation> current = current();
            invocation.thread = Thread.currentThread();
            invocation.proxy = Peer.Proxy.INSIDE;
            invocation.transaction = Peer.Transaction.ACTIVE;
            current.set(invocation);
            try {
                Connection tracked = new TrackingDataSource(source, h.peer).getConnection();
                invocation.transaction = Peer.Transaction.COMMITTED;
                assertThatThrownBy(tracked::createStatement).isInstanceOf(WeavegateCancelledException.class);
                verify(raw, org.mockito.Mockito.never()).createStatement();
                assertThat(h.peer.fatalKind).isEqualTo("transaction");
                TrackingDataSource.beginTransactionControl();
                try {
                    tracked.close();
                } finally {
                    TrackingDataSource.endTransactionControl();
                }
                assertThat(h.peer.openLeases).isZero();
            } finally {
                current.remove();
            }
        });
    }

    @TestFactory
    Stream<DynamicTest> jdbcWallClockTimeoutsAreRejected() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            DataSource source = mock(DataSource.class);
            Connection raw = mock(Connection.class);
            Statement statement = mock(Statement.class);
            when(source.getConnection()).thenReturn(raw);
            when(raw.createStatement()).thenReturn(statement);
            Peer.Invocation invocation = h.peer.new Invocation(Scripted.I1, "w1", "assign");
            ThreadLocal<Peer.Invocation> current = current();
            invocation.thread = Thread.currentThread();
            invocation.proxy = Peer.Proxy.INSIDE;
            invocation.transaction = Peer.Transaction.ACTIVE;
            current.set(invocation);
            try {
                Connection tracked = new TrackingDataSource(source, h.peer).getConnection();
                Statement trackedStatement = tracked.createStatement();
                Executor executor = Runnable::run;
                trackedStatement.setQueryTimeout(0);
                assertThatThrownBy(() -> trackedStatement.setQueryTimeout(1)).isInstanceOf(SQLException.class);
                tracked.setNetworkTimeout(executor, 0);
                assertThatThrownBy(() -> tracked.setNetworkTimeout(executor, 1)).isInstanceOf(SQLException.class);
                tracked.isValid(0);
                assertThatThrownBy(() -> tracked.isValid(1)).isInstanceOf(SQLException.class);
                verify(statement).setQueryTimeout(0);
                verify(statement, org.mockito.Mockito.never()).setQueryTimeout(1);
                verify(raw).setNetworkTimeout(executor, 0);
                verify(raw, org.mockito.Mockito.never()).setNetworkTimeout(executor, 1);
                verify(raw).isValid(0);
                verify(raw, org.mockito.Mockito.never()).isValid(1);
                trackedStatement.close();
                tracked.close();
            } finally {
                current.remove();
            }
        });
    }

    @TestFactory
    Stream<DynamicTest> retainedJdbcHandlesRejectForeignThreads() {
        return RequirementsTest.repeated(() -> {
            VectorHarness h = new VectorHarness("independent").quiet();
            DataSource source = mock(DataSource.class);
            Connection raw = mock(Connection.class);
            Statement statement = mock(Statement.class);
            ResultSet rows = mock(ResultSet.class);
            when(source.getConnection()).thenReturn(raw);
            when(raw.createStatement()).thenReturn(statement);
            when(statement.executeQuery("SELECT 1")).thenReturn(rows);
            Peer.Invocation invocation = h.peer.new Invocation(Scripted.I1, "w1", "assign");
            ThreadLocal<Peer.Invocation> current = current();
            invocation.thread = Thread.currentThread();
            invocation.proxy = Peer.Proxy.INSIDE;
            invocation.transaction = Peer.Transaction.ACTIVE;
            current.set(invocation);
            try {
                Connection tracked = new TrackingDataSource(source, h.peer).getConnection();
                Statement trackedStatement = tracked.createStatement();
                ResultSet trackedRows = trackedStatement.executeQuery("SELECT 1");
                AtomicReference<Throwable> connectionFailure = invokeOffThread(tracked::getAutoCommit);
                AtomicReference<Throwable> statementFailure = invokeOffThread(() -> trackedStatement.execute("work"));
                AtomicReference<Throwable> rowsFailure = invokeOffThread(trackedRows::next);
                assertThat(connectionFailure.get()).isInstanceOf(WeavegateCancelledException.class);
                assertThat(statementFailure.get()).isInstanceOf(WeavegateCancelledException.class);
                assertThat(rowsFailure.get()).isInstanceOf(WeavegateCancelledException.class);
                assertThat(h.peer.fatalKind).isEqualTo("protocol");
                verify(raw, org.mockito.Mockito.never()).getAutoCommit();
                verify(statement, org.mockito.Mockito.never()).execute("work");
                verify(rows, org.mockito.Mockito.never()).next();
                trackedRows.close();
                trackedStatement.close();
                tracked.close();
            } finally {
                current.remove();
            }
        });
    }

    private interface SqlCall {
        Object call() throws Exception;
    }

    private static AtomicReference<Throwable> invokeOffThread(SqlCall call) throws InterruptedException {
        AtomicReference<Throwable> failure = new AtomicReference<>();
        Thread thread = new Thread(() -> {
            try {
                call.call();
            } catch (Throwable t) {
                failure.set(t);
            }
        });
        thread.start();
        thread.join(TimeUnit.SECONDS.toMillis(20));
        assertThat(thread.isAlive()).isFalse();
        return failure;
    }

    @SuppressWarnings("unchecked")
    private static ThreadLocal<Peer.Invocation> current() {
        return (ThreadLocal<Peer.Invocation>) ReflectionTestUtils.getField(Peer.class, "CURRENT");
    }
}
