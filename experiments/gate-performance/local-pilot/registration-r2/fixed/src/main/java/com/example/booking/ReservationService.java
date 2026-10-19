package com.example.booking;

import io.github.weavegate.sdk.Weavegate;
import java.util.List;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class ReservationService {
    public static final String AFTER_SEAT_READ = "after_seat_read";
    public static final String BEFORE_RESERVATION_INSERT = "before_reservation_insert";

    private final JdbcTemplate jdbc;

    public ReservationService(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /** Reserves an open seat for a customer; returns false when the seat is closed or already held. */
    @Transactional
    public boolean reserve(long seatId, String customer) {
        List<String> open = jdbc.queryForList(
                "SELECT status FROM seat WHERE id = ? AND status = 'OPEN' FOR UPDATE", String.class, seatId);
        if (open.isEmpty()) {
            return false;
        }
        Weavegate.syncPoint(AFTER_SEAT_READ);

        Integer held = jdbc.queryForObject(
                "SELECT COUNT(*) FROM reservation WHERE seat_id = ? AND status = 'ACTIVE'",
                Integer.class, seatId);
        if (held != null && held > 0) {
            return false;
        }
        Weavegate.syncPoint(BEFORE_RESERVATION_INSERT);
        jdbc.update("INSERT INTO reservation (seat_id, customer, status) VALUES (?, ?, 'ACTIVE')",
                seatId, customer);
        return true;
    }
}
