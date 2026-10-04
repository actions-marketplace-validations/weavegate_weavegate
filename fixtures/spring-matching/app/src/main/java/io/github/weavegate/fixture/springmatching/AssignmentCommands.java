package io.github.weavegate.fixture.springmatching;

import io.github.weavegate.sdk.CommandContext;
import io.github.weavegate.sdk.Weavegate;
import io.github.weavegate.sdk.WeavegateCommand;
import java.util.List;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * The read-check-insert assignment workflow. Each command is one REQUIRED
 * transaction entered through the bean proxy. The variants differ only in the
 * request read: {@code fixed} takes a row lock with {@code SELECT ... FOR UPDATE}.
 *
 * <p>{@code assign} is the workflow under test. {@code assign_then_fail} and
 * {@code assign_then_halt} run the same workflow and then fail the transaction
 * or stop the JVM before commit; they exist only for lifecycle evidence.
 */
@Service
public class AssignmentCommands {
    static final String AFTER_READ_REQUEST = "after_read_request";
    static final String BEFORE_INSERT_ASSIGNMENT = "before_insert_assignment";

    private static final String READ_VULNERABLE =
            "SELECT status FROM project_request WHERE id = ? AND status = 'ACTIVE'";
    private static final String READ_FIXED = READ_VULNERABLE + " FOR UPDATE";

    private final JdbcTemplate jdbc;

    public AssignmentCommands(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @Transactional
    @WeavegateCommand(value = "assign", points = {AFTER_READ_REQUEST, BEFORE_INSERT_ASSIGNMENT})
    public void assign(CommandContext context) {
        workflow(context);
    }

    @Transactional
    @WeavegateCommand(value = "assign_then_fail", points = {AFTER_READ_REQUEST, BEFORE_INSERT_ASSIGNMENT})
    public void assignThenFail(CommandContext context) {
        workflow(context);
        throw new IllegalStateException("synthetic failure after the assignment insert");
    }

    @Transactional
    @WeavegateCommand(value = "assign_then_halt", points = {AFTER_READ_REQUEST, BEFORE_INSERT_ASSIGNMENT})
    public void assignThenHalt(CommandContext context) {
        workflow(context);
        // Application death with the transaction still open: no rollback,
        // pool shutdown or protocol message runs in this JVM.
        Runtime.getRuntime().halt(70);
    }

    private void workflow(CommandContext context) {
        String read = switch (context.variant()) {
            case "vulnerable" -> READ_VULNERABLE;
            case "fixed" -> READ_FIXED;
            default -> throw new IllegalArgumentException("unsupported variant");
        };
        long requestId = Long.parseLong(context.params().get("request_id"));

        List<String> active = jdbc.queryForList(read, String.class, requestId);
        if (active.isEmpty()) {
            throw new IllegalStateException("project request is not active");
        }
        Weavegate.syncPoint(AFTER_READ_REQUEST);

        Integer existing = jdbc.queryForObject(
                "SELECT COUNT(*) FROM assignment WHERE project_request_id = ? AND status = 'ACTIVE'",
                Integer.class, requestId);
        if (existing != null && existing > 0) {
            return;
        }
        Weavegate.syncPoint(BEFORE_INSERT_ASSIGNMENT);
        jdbc.update("INSERT INTO assignment (project_request_id, status) VALUES (?, 'ACTIVE')", requestId);
    }
}
