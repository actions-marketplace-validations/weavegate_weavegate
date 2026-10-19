package io.github.weavegate.fixture.springjobclaim;

import io.github.weavegate.sdk.Weavegate;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

/** A commit barrier, not a terminal or connection-return notification. */
final class CommitPoint {
    private CommitPoint() {
    }

    static void register() {
        TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
            @Override
            public void afterCommit() {
                // Still inside the command proxy on its worker thread. No JDBC
                // after commit; Spring returns the connection after this callback.
                Weavegate.syncPoint("after_commit");
            }
        });
    }
}
