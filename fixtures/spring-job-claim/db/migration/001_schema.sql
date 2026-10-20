CREATE TABLE job (
    id BIGINT PRIMARY KEY,
    epoch BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL,
    completed_epoch BIGINT NULL
) ENGINE=InnoDB;

CREATE TABLE job_receipt (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    job_id BIGINT NOT NULL,
    operation VARCHAR(16) NOT NULL,
    observed_epoch BIGINT NOT NULL,
    observed_status VARCHAR(16) NOT NULL,
    changed_rows INT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES job(id)
) ENGINE=InnoDB;
