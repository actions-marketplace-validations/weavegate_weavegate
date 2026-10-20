package com.example.booking.weavegate;

import com.example.booking.ReservationService;
import io.github.weavegate.sdk.CommandContext;
import io.github.weavegate.sdk.WeavegateCommand;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

/** Exposes the reservation workflow to weavegate workers. Inert unless WeavegateMain started the context. */
@Component
public class ReservationCommands {
    private final ReservationService reservations;

    public ReservationCommands(ReservationService reservations) {
        this.reservations = reservations;
    }

    @Transactional
    @WeavegateCommand(value = "reserve", points = {
            ReservationService.AFTER_SEAT_READ, ReservationService.BEFORE_RESERVATION_INSERT})
    public void reserve(CommandContext context) {
        reservations.reserve(Long.parseLong(context.params().get("seat_id")), context.worker());
    }
}
