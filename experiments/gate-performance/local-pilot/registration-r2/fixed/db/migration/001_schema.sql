CREATE TABLE seat (
    id     BIGINT      NOT NULL PRIMARY KEY,
    status VARCHAR(16) NOT NULL
) ENGINE = InnoDB;

CREATE TABLE reservation (
    id       BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
    seat_id  BIGINT      NOT NULL,
    customer VARCHAR(64) NOT NULL,
    status   VARCHAR(16) NOT NULL,
    KEY reservation_seat (seat_id)
) ENGINE = InnoDB;
