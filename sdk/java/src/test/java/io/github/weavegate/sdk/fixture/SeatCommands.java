package io.github.weavegate.sdk.fixture;

import io.github.weavegate.sdk.CommandContext;
import io.github.weavegate.sdk.Weavegate;
import io.github.weavegate.sdk.WeavegateCommand;
import org.springframework.dao.DataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.ConnectionCallback;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.interceptor.TransactionAspectSupport;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

/** Commands over a synthetic seat table. Each uses one REQUIRED transaction through the bean proxy. */
@Service
public class SeatCommands implements SeatCommandApi {
    private final JdbcTemplate jdbc;

    public SeatCommands(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @Transactional
    @WeavegateCommand(value = "assign", points = {"after_read", "before_write"})
    public void assign(CommandContext context) {
        jdbc.queryForObject("SELECT taken_by FROM seat WHERE id = 1 FOR UPDATE", String.class);
        Weavegate.syncPoint("after_read");
        jdbc.update("UPDATE seat SET taken_by = ? WHERE id = 1", context.worker());
        Journal.add("body-end");
    }

    @Transactional
    @WeavegateCommand(value = "navigate", points = "after_read")
    public void navigate() {
        jdbc.execute((ConnectionCallback<Void>) connection -> {
            try (var first = connection.unwrap(java.sql.Connection.class).createStatement();
                 var statement = first.getConnection().createStatement();
                 var rows = statement.unwrap(java.sql.Statement.class)
                         .executeQuery("SELECT taken_by FROM seat WHERE id = 1 FOR UPDATE")) {
                rows.next();
                Weavegate.syncPoint("after_read");
            }
            return null;
        });
    }

    @Transactional
    @WeavegateCommand("fail_body")
    public void failBody(CommandContext context) {
        jdbc.update("UPDATE seat SET taken_by = ? WHERE id = 1", context.worker());
        Journal.add("body-end");
        throw new IllegalStateException("synthetic body failure");
    }

    @Transactional
    @WeavegateCommand("rollback_only")
    public void rollbackOnly(CommandContext context) {
        jdbc.update("UPDATE seat SET taken_by = ? WHERE id = 1", context.worker());
        TransactionAspectSupport.currentTransactionStatus().setRollbackOnly();
        Journal.add("body-end");
    }

    @Transactional
    @WeavegateCommand("after_commit_failure")
    public void afterCommitFailure(CommandContext context) {
        jdbc.update("UPDATE seat SET taken_by = ? WHERE id = 1", context.worker());
        TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
            @Override
            public void afterCommit() {
                throw new IllegalStateException("synthetic after-commit failure");
            }
        });
        Journal.add("body-end");
    }

    @Transactional
    @WeavegateCommand("after_commit_jdbc")
    public void afterCommitJdbc(CommandContext context) {
        jdbc.update("UPDATE seat SET taken_by = ? WHERE id = 1", context.worker());
        TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
            @Override
            public void afterCommit() {
                jdbc.update("UPDATE seat SET taken_by = 'after' WHERE id = 1");
            }
        });
    }

    @Transactional
    @WeavegateCommand("after_completion_jdbc")
    public void afterCompletionJdbc(CommandContext context) {
        jdbc.update("UPDATE seat SET taken_by = ? WHERE id = 1", context.worker());
        TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
            @Override
            public void afterCompletion(int status) {
                jdbc.update("UPDATE seat SET taken_by = 'after' WHERE id = 1");
            }
        });
    }

    @Transactional
    @WeavegateCommand("duplicate_key")
    public void duplicateKey() {
        jdbc.update("INSERT INTO seat (id, taken_by) VALUES (1, 'duplicate')");
    }

    @Transactional
    @WeavegateCommand("caught_duplicate")
    public void caughtDuplicate() {
        try {
            jdbc.update("INSERT INTO seat (id, taken_by) VALUES (1, 'duplicate')");
        } catch (DataAccessException expected) {
            // A handled database error is application control flow, not terminal error evidence.
        }
        Journal.add("body-end");
    }

    @Transactional
    @WeavegateCommand("manual_commit")
    public void manualCommit(CommandContext context) {
        jdbc.update("UPDATE seat SET taken_by = ? WHERE id = 1", context.worker());
        jdbc.execute((ConnectionCallback<Void>) connection -> {
            connection.commit();
            return null;
        });
        throw new AssertionError("application-managed commit was not rejected");
    }

    @Transactional
    @WeavegateCommand("sql_commit")
    public void sqlCommit(CommandContext context) {
        jdbc.update("UPDATE seat SET taken_by = ? WHERE id = 1", context.worker());
        jdbc.execute("SET /* fixture */ autocommit = 1");
        throw new AssertionError("SQL transaction control was not rejected");
    }

    @Transactional
    @WeavegateCommand("implicit_commit")
    public void implicitCommit() {
        jdbc.execute("TRUNCATE TABLE seat");
        throw new AssertionError("implicit-commit SQL was not rejected");
    }

    @Transactional
    @WeavegateCommand("multi_statement")
    public void multiStatement(CommandContext context) {
        jdbc.update("UPDATE seat SET taken_by = ? WHERE id = 1", context.worker());
        jdbc.execute("SELECT 1; COMMIT");
        throw new AssertionError("multi-statement SQL was not rejected");
    }

    @Transactional
    @WeavegateCommand("session_select")
    public void sessionSelect(CommandContext context) {
        jdbc.update("UPDATE seat SET taken_by = ? WHERE id = 1", context.worker());
        jdbc.execute("SELECT 1 INTO @weavegate_fixture");
        throw new AssertionError("session-variable SQL was not rejected");
    }
}

/** Exercises reflective dispatch through a CGLIB proxy whose user class is not public. */
@Service
class PackagePrivateCommands {
    private final JdbcTemplate jdbc;

    PackagePrivateCommands(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @Transactional
    @WeavegateCommand("package_private")
    public void run(CommandContext context) {
        jdbc.update("UPDATE seat SET taken_by = ? WHERE id = 1", context.worker());
    }
}
