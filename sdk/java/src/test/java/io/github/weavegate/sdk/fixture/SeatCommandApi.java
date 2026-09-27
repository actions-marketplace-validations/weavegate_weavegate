package io.github.weavegate.sdk.fixture;

import io.github.weavegate.sdk.CommandContext;

/** Exposes the synthetic command methods when Spring selects a JDK proxy. */
public interface SeatCommandApi {
    void assign(CommandContext context);
    void navigate();
    void failBody(CommandContext context);
    void rollbackOnly(CommandContext context);
    void afterCommitFailure(CommandContext context);
    void afterCommitJdbc(CommandContext context);
    void afterCompletionJdbc(CommandContext context);
    void duplicateKey();
    void caughtDuplicate();
    void manualCommit(CommandContext context);
    void sqlCommit(CommandContext context);
    void implicitCommit();
    void multiStatement(CommandContext context);
    void sessionSelect(CommandContext context);
}
