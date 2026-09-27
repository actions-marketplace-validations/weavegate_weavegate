package io.github.weavegate.sdk;

import java.io.PrintWriter;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.sql.CallableStatement;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.SQLClientInfoException;
import java.sql.SQLFeatureNotSupportedException;
import java.sql.Statement;
import java.util.Set;
import java.util.logging.Logger;
import java.util.regex.Pattern;

import javax.sql.DataSource;

/**
 * The fixture DataSource as seen by the application. It records each lease's
 * acquisition and successful delegate {@code close()}, including close failures
 * that Spring's cleanup logs and suppresses, and registers blocking JDBC calls
 * for cancellation.
 */
final class TrackingDataSource implements DataSource {
    private static final Set<String> SESSION_MUTATORS = Set.of(
            "abort", "setCatalog", "setSchema", "setHoldability", "setTypeMap", "setClientInfo",
            "setShardingKey", "setShardingKeyIfValid", "beginRequest", "endRequest");
    private static final Set<String> RESOURCE_FACTORIES = Set.of(
            "createArrayOf", "createBlob", "createClob", "createNClob", "createSQLXML", "createStruct");
    private static final Set<String> RESULT_RESOURCES = Set.of(
            "getArray", "getBlob", "getClob", "getNClob", "getSQLXML", "getRef", "getObject",
            "getAsciiStream", "getBinaryStream", "getCharacterStream", "getNCharacterStream",
            "getUnicodeStream");
    private static final Set<String> RESULT_MUTATORS = Set.of(
            "insertRow", "deleteRow", "moveToInsertRow", "moveToCurrentRow", "cancelRowUpdates", "refreshRow");
    private static final Pattern TRANSACTION_SQL = Pattern.compile("(?is)^(?:BEGIN\\b|START\\s+TRANSACTION\\b|"
            + "COMMIT\\b|ROLLBACK\\b|SAVEPOINT\\b|RELEASE\\s+SAVEPOINT\\b|"
            + "SET\\s+(?:(?:SESSION|LOCAL|GLOBAL)\\s+)?TRANSACTION\\b|"
            + "SET\\s+(?:(?:SESSION|LOCAL|GLOBAL)\\s+)?"
            + "(?:@@\\s*(?:(?:SESSION|LOCAL|GLOBAL)\\s*\\.\\s*)?)?AUTOCOMMIT\\b|"
            + "XA\\s+(?:START|BEGIN|END|PREPARE|COMMIT|ROLLBACK)\\b)");
    private static final Pattern DYNAMIC_SQL = Pattern.compile("(?is)^(?:PREPARE\\b|EXECUTE\\b|"
            + "(?:DEALLOCATE|DROP)\\s+PREPARE\\b)");
    private static final Pattern TIMEOUT_HINT = Pattern.compile("(?is)\\bMAX_EXECUTION_TIME\\s*\\(");
    private static final Pattern NAMED_LOCK_SQL = Pattern.compile(
            "(?is)(?<![\\w$])(?:GET_LOCK|RELEASE_LOCK|RELEASE_ALL_LOCKS)\\s*\\(");
    private static final Pattern SUPPORTED_SQL = Pattern.compile("(?is)^(?:SELECT|INSERT|UPDATE|DELETE)\\b");
    private static final Pattern SESSION_EFFECT_SQL = Pattern.compile(
            "(?is)(?<![<>=!:]):=|(?<![\\w$])(?:LAST_INSERT_ID|"
            + "SLEEP|BENCHMARK|MASTER_POS_WAIT|GET_MASTER_PUBLIC_KEY)\\s*\\(");
    private static final Pattern SELECT_INTO = Pattern.compile("(?is)\\bINTO\\b");
    private static final Pattern NONTRANSACTIONAL_SQL = Pattern.compile("(?is)^(?:"
            + "(?:ALTER|ANALYZE|CACHE|CHECK|CREATE|DROP|FLUSH|GRANT|INSTALL|LOCK|OPTIMIZE|RENAME|REPAIR|"
            + "RESET|REVOKE|TRUNCATE|UNINSTALL|UNLOCK)\\b|"
            + "LOAD\\s+INDEX\\s+INTO\\s+CACHE\\b|SET\\b|CALL\\b|"
            + "\\{\\s*(?:\\?\\s*=\\s*)?CALL\\b|"
            + "CHANGE\\s+(?:MASTER|REPLICATION)\\b|(?:START|STOP)\\s+(?:REPLICA|SLAVE)\\b)");
    private static final ThreadLocal<Integer> TRANSACTION_CONTROL = new ThreadLocal<>();
    private static final ThreadLocal<Peer.Invocation> TRANSACTION_BEGIN = new ThreadLocal<>();
    private final DataSource delegate;
    private final Peer peer;

    TrackingDataSource(DataSource delegate, Peer peer) {
        this.delegate = delegate;
        this.peer = peer;
    }

    @Override
    public Connection getConnection() throws SQLException {
        Peer.Invocation invocation = Peer.current();
        if (invocation != null) {
            peer.jdbcEntry(invocation, transactionControlAllowed());
        }
        Connection connection;
        try {
            connection = delegate.getConnection();
        } catch (SQLException e) {
            observeDriverFailure(e);
            throw e;
        }
        return track(connection, invocation);
    }

    @Override
    public Connection getConnection(String username, String password) throws SQLException {
        throw new SQLFeatureNotSupportedException("fixture credentials are fixed");
    }

    private Connection track(Connection connection, Peer.Invocation invocation) throws SQLException {
        if (!peer.leaseAcquired(invocation)) {
            SQLFeatureNotSupportedException rejected =
                    new SQLFeatureNotSupportedException("database lease outside supported invocation");
            try {
                connection.close();
            } catch (SQLException closeFailure) {
                peer.unsupported("cleanup", "rejected lease return failed");
                rejected.addSuppressed(closeFailure);
            }
            throw rejected;
        }
        return (Connection) Proxy.newProxyInstance(Connection.class.getClassLoader(), new Class<?>[] {Connection.class},
                new Lease(connection, invocation));
    }

    private final class Lease implements InvocationHandler {
        private final Connection connection;
        private final Peer.Invocation invocation;
        private boolean closed;

        Lease(Connection connection, Peer.Invocation invocation) {
            this.connection = connection;
            this.invocation = invocation;
        }

        @Override
        public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
            requireInvocation(invocation);
            switch (method.getName()) {
                case "unwrap" -> {
                    return unwrapTracked(proxy, (Class<?>) args[0]);
                }
                case "isWrapperFor" -> {
                    return ((Class<?>) args[0]).isInstance(proxy);
                }
                case "close" -> {
                    synchronized (this) {
                        if (closed) {
                            return null;
                        }
                        closed = true;
                    }
                    try {
                        connection.close();
                    } catch (Throwable t) {
                        peer.leaseCloseFailed(invocation);
                        throw t;
                    }
                    peer.leaseReturned(invocation);
                    return null;
                }
                case "isClosed" -> {
                    synchronized (this) {
                        if (closed) {
                            return true;
                        }
                    }
                }
                case "equals" -> {
                    return proxy == args[0];
                }
                case "hashCode" -> {
                    return System.identityHashCode(proxy);
                }
                case "toString" -> {
                    return "TrackedConnection";
                }
                default -> {
                }
            }
            if (transactionControl(method.getName()) && !transactionControlAllowed()) {
                throw new SQLFeatureNotSupportedException("application-managed transaction control is unsupported");
            }
            if ((SESSION_MUTATORS.contains(method.getName()) || RESOURCE_FACTORIES.contains(method.getName())
                    || method.getName().equals("setReadOnly") || method.getName().equals("setTransactionIsolation"))
                    && !transactionControlAllowed()) {
                if (method.getName().equals("setClientInfo")) {
                    throw new SQLClientInfoException("application-managed session state is unsupported", null);
                }
                throw new SQLFeatureNotSupportedException("application-managed session state or JDBC resources are unsupported");
            }
            rejectUnsupportedSql(method, args);
            rejectNetworkTimeout(method, args);
            rejectValidationTimeout(method, args);
            if (method.getName().equals("getMetaData")) {
                throw new SQLFeatureNotSupportedException("JDBC metadata access is unsupported");
            }
            Object result = call(connection, method, args);
            if (result instanceof Statement statement) {
                return trackStatement(statement, invocation, (Connection) proxy);
            }
            return result;
        }
    }

    private Statement trackStatement(Statement statement, Peer.Invocation invocation, Connection connection) {
        Class<?> type = statement instanceof CallableStatement ? CallableStatement.class
                : statement instanceof PreparedStatement ? PreparedStatement.class : Statement.class;
        return (Statement) Proxy.newProxyInstance(Statement.class.getClassLoader(), new Class<?>[] {type},
                new Executing(statement, invocation, connection));
    }

    private ResultSet trackRows(ResultSet rows, Statement statement, Peer.Invocation invocation,
                                Seams.Cancellable cancel) {
        return (ResultSet) Proxy.newProxyInstance(ResultSet.class.getClassLoader(), new Class<?>[] {ResultSet.class},
                (proxy, method, args) -> {
                    requireInvocation(invocation);
                    switch (method.getName()) {
                        case "getStatement" -> { return statement; }
                        case "unwrap" -> { return unwrapTracked(proxy, (Class<?>) args[0]); }
                        case "isWrapperFor" -> { return ((Class<?>) args[0]).isInstance(proxy); }
                        case "equals" -> { return proxy == args[0]; }
                        case "hashCode" -> { return System.identityHashCode(proxy); }
                        case "toString" -> { return "TrackedResultSet"; }
                        default -> { }
                    }
                    if (RESULT_RESOURCES.contains(method.getName()) || method.getName().startsWith("update")
                            || RESULT_MUTATORS.contains(method.getName())) {
                        throw new SQLFeatureNotSupportedException("result-set resources and mutations are unsupported");
                    }
                    if (invocation == null || cancel == null) {
                        return call(rows, method, args);
                    }
                    try {
                        if (!peer.statementStarted(invocation, cancel) && !method.getName().equals("close")) {
                            throw new WeavegateCancelledException("cancelled before result-set navigation");
                        }
                        return call(rows, method, args);
                    } finally {
                        peer.statementFinished(invocation, cancel);
                    }
                });
    }

    private static Object unwrapTracked(Object proxy, Class<?> type) throws SQLException {
        if (type.isInstance(proxy)) {
            return proxy;
        }
        throw new SQLFeatureNotSupportedException("vendor JDBC unwrapping bypasses weavegate tracking");
    }

    private final class Executing implements InvocationHandler {
        private final Statement statement;
        private final Peer.Invocation invocation;
        private final Connection connection;
        private final Seams.Cancellable cancel;

        Executing(Statement statement, Peer.Invocation invocation, Connection connection) {
            this.statement = statement;
            this.invocation = invocation;
            this.connection = connection;
            this.cancel = statement::cancel;
        }

        @Override
        public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
            requireInvocation(invocation);
            switch (method.getName()) {
                case "getConnection" -> { return connection; }
                case "unwrap" -> { return unwrapTracked(proxy, (Class<?>) args[0]); }
                case "isWrapperFor" -> { return ((Class<?>) args[0]).isInstance(proxy); }
                case "equals" -> { return proxy == args[0]; }
                case "hashCode" -> { return System.identityHashCode(proxy); }
                case "toString" -> { return "TrackedStatement"; }
                default -> { }
            }
            rejectUnsupportedSql(method, args);
            if (method.getName().equals("addBatch") || method.getName().equals("executeBatch")
                    || method.getName().equals("executeLargeBatch")) {
                throw new SQLFeatureNotSupportedException("JDBC batch execution is unsupported");
            }
            rejectQueryTimeout(method, args);
            if (method.getName().equals("getMetaData") || method.getName().equals("getParameterMetaData")) {
                throw new SQLFeatureNotSupportedException("JDBC metadata access is unsupported");
            }
            if (method.getName().equals("closeOnCompletion")) {
                throw new SQLFeatureNotSupportedException("untracked automatic statement close is unsupported");
            }
            boolean cancellable = method.getName().startsWith("execute") || method.getName().equals("close")
                    || method.getName().equals("getMoreResults");
            if (invocation == null || !cancellable) {
                Object value = call(statement, method, args);
                return value instanceof ResultSet rows
                        ? trackRows(rows, (Statement) proxy, invocation, cancel) : value;
            }
            try {
                if (!peer.statementStarted(invocation, cancel) && !method.getName().equals("close")) {
                    throw new WeavegateCancelledException("cancelled before statement");
                }
                Object value = call(statement, method, args);
                return value instanceof ResultSet rows
                        ? trackRows(rows, (Statement) proxy, invocation, cancel) : value;
            } finally {
                peer.statementFinished(invocation, cancel);
            }
        }
    }

    private Object call(Object target, Method method, Object[] args) throws Throwable {
        try {
            return method.invoke(target, args);
        } catch (InvocationTargetException e) {
            if (e.getCause() instanceof SQLException sql) {
                observeDriverFailure(sql);
            }
            throw e.getCause();
        }
    }

    private void observeDriverFailure(SQLException failure) {
        Peer.Invocation invocation = Peer.current();
        if (invocation != null) {
            peer.driverFailure(invocation, failure, driverSummary(failure));
            if (TRANSACTION_BEGIN.get() == invocation) {
                // Begin failures cannot be caught by command code. Preserve their
                // order before Spring returns the lease and cancellation can race.
                peer.recordSource(invocation, failure);
            }
        }
    }

    private void requireInvocation(Peer.Invocation invocation) {
        if (invocation != null) {
            peer.jdbcEntry(invocation, transactionControlAllowed());
        }
    }

    private void rejectUnsupportedSql(Method method, Object[] args) throws SQLException {
        if (method.getName().equals("prepareCall")) {
            peer.unsupported("transaction", "stored procedures are unsupported");
            throw new SQLFeatureNotSupportedException("stored procedures are unsupported");
        }
        if (args == null || args.length == 0 || !(args[0] instanceof String sql)) {
            return;
        }
        String name = method.getName();
        if (name.startsWith("execute") || name.equals("addBatch") || name.startsWith("prepare")) {
            String normalized = normalizeSql(sql).stripLeading();
            String code = unquotedSql(normalized);
            if (NAMED_LOCK_SQL.matcher(code).find()) {
                throw new SQLFeatureNotSupportedException("session-scoped named locks are unsupported");
            }
            if (DYNAMIC_SQL.matcher(normalized).find()) {
                throw new SQLFeatureNotSupportedException("server-side prepared SQL is unsupported");
            }
            if (TRANSACTION_SQL.matcher(normalized).find()) {
                throw new SQLFeatureNotSupportedException("application-managed transaction control is unsupported");
            }
            if (normalized.contains(" WG_TIMEOUT_HINT ")) {
                throw new SQLFeatureNotSupportedException("SQL wall-clock timeouts are unsupported");
            }
            if (NONTRANSACTIONAL_SQL.matcher(normalized).find()) {
                peer.unsupported("transaction", "nontransactional SQL is unsupported");
                throw new SQLFeatureNotSupportedException("nontransactional SQL is unsupported");
            }
            // This is deliberately a small admission rule, not a MySQL parser.
            // The application and fixture must meet the SQL obligations in the
            // Java reference; guard the paths that can escape a transaction here.
            if (!SUPPORTED_SQL.matcher(code).find() || code.indexOf(';') >= 0 || code.indexOf('@') >= 0
                    || SESSION_EFFECT_SQL.matcher(code).find()
                    || (code.regionMatches(true, 0, "SELECT", 0, 6) && SELECT_INTO.matcher(code).find())) {
                peer.unsupported("transaction", "SQL outside the supported subset");
                throw new SQLFeatureNotSupportedException("SQL outside the supported subset");
            }
        }
    }

    private static void rejectQueryTimeout(Method method, Object[] args) throws SQLException {
        if (method.getName().equals("setQueryTimeout") && args != null && args.length == 1
                && args[0] instanceof Integer seconds && seconds != 0) {
            throw new SQLFeatureNotSupportedException("wall-clock JDBC query timeouts are unsupported");
        }
    }

    private static void rejectNetworkTimeout(Method method, Object[] args) throws SQLException {
        if (method.getName().equals("setNetworkTimeout") && args != null && args.length == 2
                && args[1] instanceof Integer milliseconds && milliseconds != 0) {
            throw new SQLFeatureNotSupportedException("wall-clock JDBC network timeouts are unsupported");
        }
    }

    private static void rejectValidationTimeout(Method method, Object[] args) throws SQLException {
        if (method.getName().equals("isValid") && args != null && args.length == 1
                && args[0] instanceof Integer seconds && seconds != 0) {
            throw new SQLFeatureNotSupportedException("wall-clock JDBC validation timeouts are unsupported");
        }
    }

    /** Removes MySQL comments outside quoted values; executable comments retain their SQL body. */
    private static String normalizeSql(String sql) {
        StringBuilder normalized = new StringBuilder(sql.length());
        for (int i = 0; i < sql.length();) {
            char current = sql.charAt(i);
            if (current == '\'' || current == '"' || current == '`') {
                i = appendQuoted(sql, i, normalized);
                continue;
            }
            if (current == '#' || (current == '-' && i + 2 < sql.length()
                    && sql.charAt(i + 1) == '-' && Character.isWhitespace(sql.charAt(i + 2)))) {
                int line = i;
                while (line < sql.length() && sql.charAt(line) != '\n' && sql.charAt(line) != '\r') {
                    line++;
                }
                normalized.append(' ');
                i = line;
                continue;
            }
            if (current == '/' && i + 1 < sql.length() && sql.charAt(i + 1) == '*') {
                int end = sql.indexOf("*/", i + 2);
                if (end < 0) {
                    normalized.append(sql, i, sql.length());
                    break;
                }
                if (i + 2 < sql.length() && sql.charAt(i + 2) == '!') {
                    String executable = sql.substring(i + 3, end).stripLeading()
                            .replaceFirst("^\\d{5,6}\\s*", "");
                    normalized.append(' ').append(normalizeSql(executable)).append(' ');
                } else if (i + 2 < sql.length() && sql.charAt(i + 2) == '+'
                        && TIMEOUT_HINT.matcher(sql.substring(i + 3, end)).find()) {
                    normalized.append(" WG_TIMEOUT_HINT ");
                } else {
                    normalized.append(' ');
                }
                i = end + 2;
                continue;
            }
            normalized.append(current);
            i++;
        }
        return normalized.toString();
    }

    private static int appendQuoted(String sql, int offset, StringBuilder normalized) {
        char quote = sql.charAt(offset);
        normalized.append(quote);
        int i = offset + 1;
        while (i < sql.length()) {
            char current = sql.charAt(i);
            normalized.append(current);
            i++;
            if (current == '\\' && i < sql.length()) {
                normalized.append(sql.charAt(i++));
            } else if (current == quote) {
                if (i < sql.length() && sql.charAt(i) == quote) {
                    normalized.append(sql.charAt(i++));
                } else {
                    break;
                }
            }
        }
        return i;
    }

    /** Inspect SQL operations without treating quoted data as a function call. */
    private static String unquotedSql(String sql) {
        StringBuilder code = new StringBuilder(sql.length());
        for (int i = 0; i < sql.length();) {
            char c = sql.charAt(i);
            if (c == '\'' || c == '"' || c == '`') {
                i = appendQuoted(sql, i, new StringBuilder());
                code.append(' ');
            } else {
                code.append(c);
                i++;
            }
        }
        return code.toString();
    }

    private static boolean transactionControl(String method) {
        return method.equals("setAutoCommit") || method.equals("commit") || method.equals("rollback")
                || method.equals("setSavepoint") || method.equals("releaseSavepoint");
    }

    private static boolean transactionControlAllowed() {
        return TRANSACTION_CONTROL.get() != null;
    }

    static void beginTransactionControl() {
        Integer depth = TRANSACTION_CONTROL.get();
        TRANSACTION_CONTROL.set(depth == null ? 1 : depth + 1);
    }

    static void beginTransactionAttempt(Peer.Invocation invocation) {
        TRANSACTION_BEGIN.set(invocation);
    }

    static void endTransactionAttempt() {
        TRANSACTION_BEGIN.remove();
    }

    static void endTransactionControl() {
        Integer depth = TRANSACTION_CONTROL.get();
        if (depth == null || depth == 1) {
            TRANSACTION_CONTROL.remove();
        } else {
            TRANSACTION_CONTROL.set(depth - 1);
        }
    }

    /** Convert an escaping driver exception to fixed wire-safe evidence. */
    static SQLException driverSummary(SQLException failure) {
        var classified = Peer.classify(failure);
        boolean mysql = classified.get("kind").stringValue().equals("mysql");
        return new SQLException(mysql ? "MySQL operation failed" : "database operation failed",
                classified.get("sql_state").stringValue(), classified.get("mysql_code").intValue());
    }

    @Override
    public PrintWriter getLogWriter() throws SQLException {
        return delegate.getLogWriter();
    }

    @Override
    public void setLogWriter(PrintWriter out) throws SQLException {
        delegate.setLogWriter(out);
    }

    @Override
    public void setLoginTimeout(int seconds) throws SQLException {
        if (seconds != 0) {
            throw new SQLFeatureNotSupportedException("wall-clock DataSource login timeouts are unsupported");
        }
        delegate.setLoginTimeout(seconds);
    }

    @Override
    public int getLoginTimeout() throws SQLException {
        return delegate.getLoginTimeout();
    }

    @Override
    public Logger getParentLogger() throws SQLFeatureNotSupportedException {
        return delegate.getParentLogger();
    }

    @Override
    public <T> T unwrap(Class<T> type) throws SQLException {
        if (type.isInstance(this)) {
            return type.cast(this);
        }
        throw new SQLException("tracked DataSource cannot be unwrapped");
    }

    @Override
    public boolean isWrapperFor(Class<?> type) {
        return type.isInstance(this);
    }
}
