CREATE TABLE quota_account (
    id BIGINT PRIMARY KEY,
    capacity INT NOT NULL,
    remaining INT NOT NULL
) ENGINE=InnoDB;

CREATE TABLE quota_receipt (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    account_id BIGINT NOT NULL,
    units INT NOT NULL,
    observed_remaining INT NOT NULL,
    accepted BOOLEAN NOT NULL,
    FOREIGN KEY (account_id) REFERENCES quota_account(id)
) ENGINE=InnoDB;
