package fixture

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"strconv"
	"strings"
	"sync"
	"time"

	mysqldriver "github.com/go-sql-driver/mysql"
	mysqlcontainer "github.com/testcontainers/testcontainers-go/modules/mysql"
)

const (
	fixtureDatabase               = "weavegate"
	fixtureUsername               = "weavegate"
	rootUsername                  = "root"
	failedProvisionCleanupTimeout = 30 * time.Second
	// Leave most of the cleanup deadline for terminating the external
	// container: a local pool can be abandoned on process exit, while a
	// leaked container remains an external resource.
	poolCloseTimeout = 5 * time.Second
)

type mysqlFixture struct {
	mu sync.Mutex

	container                   *mysqlcontainer.MySQLContainer
	admin                       *sql.DB
	db                          *DB
	prepared                    Prepared
	provisioned                 bool
	quarantined                 error
	quarantineTeardownSucceeded bool
	adminPassword               string
	applicationPassword         string
	terminateContainer          func(context.Context, *mysqlcontainer.MySQLContainer) error
}

// NewMySQLFixture returns a fixture backed by a Testcontainers MySQL instance.
func NewMySQLFixture() Provisioner {
	return &mysqlFixture{terminateContainer: terminateContainer}
}

func (f *mysqlFixture) Provision(
	ctx context.Context,
	prepared Prepared,
) (handle *DB, returnErr error) {
	f.mu.Lock()
	defer f.mu.Unlock()

	if f.provisioned {
		return nil, fmt.Errorf("provision MySQL fixture: already provisioned")
	}
	if f.container != nil {
		return nil, fmt.Errorf("provision MySQL fixture: cleanup pending; call Teardown before provisioning again")
	}
	if f.quarantined != nil && !f.quarantineTeardownSucceeded {
		return nil, fmt.Errorf("provision MySQL fixture: quarantined cleanup pending; call Teardown before provisioning again: %w", ErrQuarantined)
	}
	if !prepared.valid || strings.TrimSpace(prepared.image) == "" {
		return nil, fmt.Errorf("provision MySQL fixture: prepared fixture is required")
	}
	adminPassword, err := randomCredential()
	if err != nil {
		return nil, fmt.Errorf("provision MySQL fixture: generate administrative credential: %w", err)
	}
	applicationPassword, err := randomCredential()
	if err != nil {
		return nil, fmt.Errorf("provision MySQL fixture: generate application credential: %w", err)
	}
	if applicationPassword == adminPassword {
		return nil, fmt.Errorf("provision MySQL fixture: generated credentials are not distinct")
	}
	defer func() {
		returnErr = redactConnectionError(returnErr, adminPassword, applicationPassword)
		if returnErr != nil {
			handle = nil
			if f.container != nil {
				f.adminPassword = adminPassword
				f.applicationPassword = applicationPassword
			}
		}
	}()

	container, err := mysqlcontainer.Run(
		ctx,
		prepared.image,
		mysqlcontainer.WithDatabase(fixtureDatabase),
		mysqlcontainer.WithUsername(rootUsername),
		mysqlcontainer.WithPassword(adminPassword),
	)
	if err != nil {
		if container != nil {
			return nil, errors.Join(
				fmt.Errorf("provision MySQL fixture: start container: %w", err),
				f.cleanupFailedProvision(ctx, nil, nil, container),
			)
		}
		return nil, fmt.Errorf("provision MySQL fixture: start container: %w", err)
	}

	var admin *sql.DB
	var app *sql.DB
	defer func() {
		if returnErr == nil {
			return
		}

		returnErr = errors.Join(
			returnErr,
			f.cleanupFailedProvision(ctx, app, admin, container),
		)
	}()

	admin, err = openAdminDatabase(ctx, container, adminPassword)
	if err != nil {
		return nil, fmt.Errorf("provision MySQL fixture: %w", err)
	}

	if err := createApplicationUser(ctx, admin, applicationPassword); err != nil {
		return nil, fmt.Errorf("provision MySQL fixture: %w", err)
	}
	descriptor, err := newMySQLConnectionDescriptor(ctx, container, applicationPassword)
	if err != nil {
		return nil, fmt.Errorf("provision MySQL fixture: %w", err)
	}
	app, err = openApplicationDatabase(ctx, descriptor)
	if err != nil {
		return nil, fmt.Errorf("provision MySQL fixture: %w", err)
	}
	if err := applyFixtureSQL(ctx, app, prepared); err != nil {
		return nil, fmt.Errorf("provision MySQL fixture: apply SQL: %w", err)
	}

	handle = &DB{SQL: app, Connection: descriptor}
	f.container = container
	f.admin = admin
	f.db = handle
	f.prepared = prepared.clone()
	f.provisioned = true
	f.quarantined = nil
	f.quarantineTeardownSucceeded = false
	f.adminPassword = adminPassword
	f.applicationPassword = applicationPassword

	return handle, nil
}

func (f *mysqlFixture) cleanupFailedProvision(
	operationCtx context.Context,
	app *sql.DB,
	admin *sql.DB,
	container *mysqlcontainer.MySQLContainer,
) error {
	return withProvisionCleanupContext(operationCtx, func(cleanupCtx context.Context) error {
		appErr := closeDatabaseWithin(cleanupCtx, poolCloseTimeout, "application database", app)
		adminErr := closeDatabaseWithin(cleanupCtx, poolCloseTimeout, "administrative database", admin)
		terminateErr := f.terminate(cleanupCtx, container)
		if terminateErr != nil && container != nil {
			f.container = container
		}

		return errors.Join(appErr, adminErr, terminateErr)
	})
}

func (f *mysqlFixture) Ready(db *DB) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	var err error
	switch {
	case f.quarantined != nil:
		err = errors.Join(ErrQuarantined, f.quarantined)
	case !f.provisioned:
		err = errors.New("not provisioned")
	case db == nil || db != f.db || !db.Connection.Valid():
		err = ErrConnectionDescriptorInvalid
	}
	if err != nil {
		return redactConnectionError(fmt.Errorf("fixture unavailable: %w", err), f.adminPassword, f.applicationPassword)
	}
	return nil
}

func withProvisionCleanupContext(
	operationCtx context.Context,
	cleanup func(context.Context) error,
) error {
	cleanupCtx, cancel := context.WithTimeout(
		context.WithoutCancel(operationCtx),
		failedProvisionCleanupTimeout,
	)
	defer cancel()

	return cleanup(cleanupCtx)
}

func (f *mysqlFixture) Reset(ctx context.Context) (returnErr error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	defer func() {
		returnErr = redactConnectionError(returnErr, f.adminPassword, f.applicationPassword)
	}()
	if f.quarantined != nil {
		return fmt.Errorf("reset MySQL fixture: %w", errors.Join(ErrQuarantined, f.quarantined))
	}

	if !f.provisioned {
		return fmt.Errorf("reset MySQL fixture: not provisioned")
	}
	if f.db == nil || !f.db.Connection.Valid() {
		return fmt.Errorf("reset MySQL fixture: connection descriptor invalid; call Teardown before reuse")
	}
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("reset MySQL fixture: %w", err)
	}

	// Reset runs only after every worker is terminal, so unlike teardown it
	// does not need to abandon a pool close to preserve a cleanup deadline.
	if err := closeDatabase("application database", f.db.SQL); err != nil {
		f.db.Connection.invalidate()
		return fmt.Errorf("reset MySQL fixture: %w", err)
	}
	f.db.SQL = nil

	if _, err := f.admin.ExecContext(ctx, "DROP DATABASE IF EXISTS `"+fixtureDatabase+"`"); err != nil {
		f.db.Connection.invalidate()
		return fmt.Errorf("reset MySQL fixture: drop database: %w", err)
	}
	if _, err := f.admin.ExecContext(ctx, "CREATE DATABASE `"+fixtureDatabase+"`"); err != nil {
		f.db.Connection.invalidate()
		return fmt.Errorf("reset MySQL fixture: create database: %w", err)
	}

	app, err := openApplicationDatabase(ctx, f.db.Connection)
	if err != nil {
		f.db.Connection.invalidate()
		return fmt.Errorf("reset MySQL fixture: %w", err)
	}
	if err := applyFixtureSQL(ctx, app, f.prepared); err != nil {
		f.db.Connection.invalidate()
		return errors.Join(
			fmt.Errorf("reset MySQL fixture: apply SQL: %w", err),
			closeDatabase("application database", app),
		)
	}

	f.db.SQL = app
	return nil
}

func (f *mysqlFixture) Quarantine(cause error) {
	if cause == nil {
		return
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.quarantined == nil {
		f.quarantined = cause
	}
	f.quarantineTeardownSucceeded = false
	if f.db != nil {
		f.db.Connection.invalidate()
	}
}

func (f *mysqlFixture) Teardown(ctx context.Context) (returnErr error) {
	if ctx == nil {
		return errors.New("teardown MySQL fixture: context is required")
	}
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("teardown MySQL fixture: %w", err)
	}

	f.mu.Lock()
	defer f.mu.Unlock()
	adminPassword := f.adminPassword
	applicationPassword := f.applicationPassword
	defer func() {
		returnErr = redactConnectionError(returnErr, adminPassword, applicationPassword)
	}()
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("teardown MySQL fixture: %w", err)
	}

	if !f.provisioned && f.container == nil && f.admin == nil && f.db == nil {
		f.quarantineTeardownSucceeded = true
		return nil
	}

	var app *sql.DB
	if f.db != nil {
		f.db.Connection.invalidate()
		app = f.db.SQL
		f.db.SQL = nil
	}

	appErr := closeDatabaseWithin(ctx, poolCloseTimeout, "application database", app)
	adminErr := closeDatabaseWithin(ctx, poolCloseTimeout, "administrative database", f.admin)
	terminateErr := f.terminate(ctx, f.container)
	if terminateErr != nil {
		terminateErr = fmt.Errorf("terminate MySQL container: %w", terminateErr)
	}
	err := errors.Join(appErr, adminErr, terminateErr)

	if terminateErr != nil {
		return err
	}

	f.container = nil
	f.admin = nil
	f.db = nil
	f.prepared = Prepared{}
	f.provisioned = false
	f.adminPassword = ""
	f.applicationPassword = ""
	if err == nil {
		f.quarantineTeardownSucceeded = true
	}

	return err
}

func (f *mysqlFixture) terminate(
	ctx context.Context,
	container *mysqlcontainer.MySQLContainer,
) error {
	if f.terminateContainer == nil {
		return terminateContainer(ctx, container)
	}

	return f.terminateContainer(ctx, container)
}

func openAdminDatabase(
	ctx context.Context,
	container *mysqlcontainer.MySQLContainer,
	password string,
) (*sql.DB, error) {
	host, port, err := mysqlEndpoint(ctx, container)
	if err != nil {
		return nil, fmt.Errorf("get administrative endpoint: %w", err)
	}

	config := mysqldriver.NewConfig()
	config.User = rootUsername
	config.Passwd = password
	config.Net = "tcp"
	config.Addr = net.JoinHostPort(host, strconv.Itoa(port))
	config.DBName = fixtureDatabase

	db, err := sql.Open("mysql", config.FormatDSN())
	if err != nil {
		return nil, fmt.Errorf("open administrative database: %w", err)
	}
	if err := db.PingContext(ctx); err != nil {
		return nil, errors.Join(
			fmt.Errorf("ping administrative database: %w", err),
			closeDatabase("administrative database", db),
		)
	}

	return db, nil
}

func createApplicationUser(ctx context.Context, admin *sql.DB, password string) error {
	// randomCredential returns lowercase hexadecimal text, so the generated
	// value cannot terminate or escape this single-quoted MySQL literal.
	if _, err := admin.ExecContext(
		ctx,
		"CREATE USER '"+fixtureUsername+"'@'%' IDENTIFIED BY '"+password+"'",
	); err != nil {
		return fmt.Errorf("create application user: %w", err)
	}
	if _, err := admin.ExecContext(
		ctx,
		"GRANT ALL PRIVILEGES ON `"+fixtureDatabase+"`.* TO '"+fixtureUsername+"'@'%'",
	); err != nil {
		return fmt.Errorf("grant application database access: %w", err)
	}
	return nil
}

func newMySQLConnectionDescriptor(
	ctx context.Context,
	container *mysqlcontainer.MySQLContainer,
	password string,
) (ConnectionDescriptor, error) {
	host, port, err := mysqlEndpoint(ctx, container)
	if err != nil {
		return ConnectionDescriptor{}, fmt.Errorf("get application endpoint: %w", err)
	}
	return newConnectionDescriptor(
		"mysql",
		host,
		port,
		fixtureDatabase,
		fixtureUsername,
		password,
	), nil
}

func mysqlEndpoint(
	ctx context.Context,
	container *mysqlcontainer.MySQLContainer,
) (string, int, error) {
	host, err := container.Host(ctx)
	if err != nil {
		return "", 0, err
	}
	mappedPort, err := container.MappedPort(ctx, "3306/tcp")
	if err != nil {
		return "", 0, err
	}
	port := int(mappedPort.Num())
	if port < 1 || port > 65535 {
		return "", 0, fmt.Errorf("mapped port %d is outside 1-65535", port)
	}
	return host, port, nil
}

func openApplicationDatabase(
	ctx context.Context,
	descriptor ConnectionDescriptor,
) (*sql.DB, error) {
	password, err := descriptor.Password()
	if err != nil {
		return nil, fmt.Errorf("get application credential: %w", err)
	}
	config := mysqldriver.NewConfig()
	config.User = descriptor.Username
	config.Passwd = password
	config.Net = "tcp"
	config.Addr = net.JoinHostPort(descriptor.Host, strconv.Itoa(descriptor.Port))
	config.DBName = descriptor.Name
	config.ParseTime = true

	db, err := sql.Open("mysql", config.FormatDSN())
	if err != nil {
		return nil, fmt.Errorf("open application database: %w", err)
	}
	if err := db.PingContext(ctx); err != nil {
		return nil, errors.Join(
			fmt.Errorf("ping application database: %w", err),
			closeDatabase("application database", db),
		)
	}

	return db, nil
}

func randomCredential() (string, error) {
	bytes := make([]byte, 32)
	if _, err := rand.Read(bytes); err != nil {
		return "", err
	}
	return hex.EncodeToString(bytes), nil
}

func closeDatabase(name string, db *sql.DB) error {
	if db == nil {
		return nil
	}
	if err := db.Close(); err != nil {
		return fmt.Errorf("close %s: %w", name, err)
	}

	return nil
}

func closeDatabaseWithin(
	ctx context.Context,
	timeout time.Duration,
	name string,
	db *sql.DB,
) error {
	if db == nil {
		return nil
	}

	closed := make(chan error, 1)
	go func() {
		closed <- db.Close()
	}()

	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case err := <-closed:
		if err != nil {
			return fmt.Errorf("close %s: %w", name, err)
		}
		return nil
	case <-ctx.Done():
		return fmt.Errorf("close %s: exceeded teardown budget: %w", name, ctx.Err())
	case <-timer.C:
		return fmt.Errorf("close %s: exceeded %v teardown budget", name, timeout)
	}
}

func terminateContainer(
	ctx context.Context,
	container *mysqlcontainer.MySQLContainer,
) error {
	if container == nil {
		return nil
	}
	if err := container.Terminate(ctx); err != nil {
		return fmt.Errorf("terminate MySQL container: %w", err)
	}

	return nil
}
