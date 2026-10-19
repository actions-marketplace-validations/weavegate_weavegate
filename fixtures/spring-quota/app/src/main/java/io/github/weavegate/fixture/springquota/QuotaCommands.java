package io.github.weavegate.fixture.springquota;

import io.github.weavegate.sdk.CommandContext;
import io.github.weavegate.sdk.Weavegate;
import io.github.weavegate.sdk.WeavegateCommand;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Isolation;
import org.springframework.transaction.annotation.Transactional;

/** Two requests spend the same finite quota; receipts record accepted units. */
@Service
public class QuotaCommands {
    private final JdbcTemplate jdbc;

    public QuotaCommands(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @Transactional(isolation = Isolation.REPEATABLE_READ)
    @WeavegateCommand(value = "reserve", points = {
            "before_read", "after_read", "before_write", "after_commit"})
    public void reserve(CommandContext context) {
        long account = Long.parseLong(context.params().get("account_id"));
        int units = Integer.parseInt(context.params().get("units"));
        if (units <= 0) {
            throw new IllegalArgumentException("units must be positive");
        }
        boolean fixed = switch (context.variant()) {
            case "vulnerable" -> false;
            case "fixed" -> true;
            default -> throw new IllegalArgumentException("unsupported variant");
        };
        CommitPoint.register();
        Weavegate.syncPoint("before_read");
        // First consistent read: this statement establishes the RR snapshot.
        int observed = jdbc.queryForObject(
                "SELECT remaining FROM quota_account WHERE id = ?", Integer.class, account);
        Weavegate.syncPoint("after_read");
        Weavegate.syncPoint("before_write");

        boolean accepted;
        if (fixed) {
            // Recheck and decrement the current row atomically, not the snapshot.
            accepted = jdbc.update("UPDATE quota_account SET remaining = remaining - ? "
                    + "WHERE id = ? AND remaining >= ?", units, account, units) == 1;
        } else {
            accepted = observed >= units;
            if (accepted) {
                jdbc.update("UPDATE quota_account SET remaining = ? WHERE id = ?", observed - units, account);
            }
        }
        jdbc.update("INSERT INTO quota_receipt (account_id, units, observed_remaining, accepted) VALUES (?, ?, ?, ?)",
                account, units, observed, accepted);
    }
}
