CREATE TABLE project_request (
    id BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB;

CREATE TABLE assignment (
    id BIGINT NOT NULL AUTO_INCREMENT,
    project_request_id BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL,
    PRIMARY KEY (id),
    INDEX idx_assignment_project_request (project_request_id),
    CONSTRAINT fk_assignment_project_request
        FOREIGN KEY (project_request_id) REFERENCES project_request (id)
) ENGINE=InnoDB;
