package io.github.weavegate.fixture.springjobclaim;

import io.github.weavegate.sdk.CommandContext;
import io.github.weavegate.sdk.Weavegate;
import io.github.weavegate.sdk.WeavegateCommand;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Isolation;
import org.springframework.transaction.annotation.Transactional;

/** A stale lease holder completes a job while another command reassigns it. */
@Service
public class JobCommands {
    private record Snapshot(long epoch, String status) {
    }

    private final JdbcTemplate jdbc;

    public JobCommands(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @Transactional(isolation = Isolation.REPEATABLE_READ)
    @WeavegateCommand(value = "complete", points = {
            "before_read", "after_read", "before_write", "after_commit"})
    public void complete(CommandContext context) {
        workflow(context, true);
    }

    @Transactional(isolation = Isolation.REPEATABLE_READ)
    @WeavegateCommand(value = "reassign", points = {
            "before_read", "after_read", "before_write", "after_commit"})
    public void reassign(CommandContext context) {
        workflow(context, false);
    }

    private void workflow(CommandContext context, boolean complete) {
        long job = Long.parseLong(context.params().get("job_id"));
        long expected = Long.parseLong(context.params().get("expected_epoch"));
        boolean fixed = switch (context.variant()) {
            case "vulnerable" -> false;
            case "fixed" -> true;
            default -> throw new IllegalArgumentException("unsupported variant");
        };
        CommitPoint.register();
        Weavegate.syncPoint("before_read");
        // First consistent read: no table is read before this RR snapshot.
        Snapshot observed = jdbc.queryForObject("SELECT epoch, status FROM job WHERE id = ?",
                (rs, row) -> new Snapshot(rs.getLong("epoch"), rs.getString("status")), job);
        Weavegate.syncPoint("after_read");
        Weavegate.syncPoint("before_write");

        int changed = 0;
        if (observed.epoch() == expected && observed.status().equals("RUNNING")) {
            if (complete) {
                String sql = "UPDATE job SET status = 'DONE', completed_epoch = ? WHERE id = ?";
                changed = fixed
                        ? jdbc.update(sql + " AND epoch = ? AND status = 'RUNNING'", expected, job, expected)
                        : jdbc.update(sql, expected, job);
            } else {
                // Reassign only a still-running lease. This path is identical in both variants.
                changed = jdbc.update("UPDATE job SET epoch = epoch + 1 WHERE id = ? "
                        + "AND epoch = ? AND status = 'RUNNING'", job, expected);
            }
        }
        jdbc.update("INSERT INTO job_receipt (job_id, operation, observed_epoch, observed_status, changed_rows) "
                + "VALUES (?, ?, ?, ?, ?)", job, complete ? "complete" : "reassign",
                observed.epoch(), observed.status(), changed);
    }
}
