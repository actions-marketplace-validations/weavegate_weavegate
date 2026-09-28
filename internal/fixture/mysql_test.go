package fixture

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	mysqldriver "github.com/go-sql-driver/mysql"
	mysqlcontainer "github.com/testcontainers/testcontainers-go/modules/mysql"
)

var blockingCloseDriverSequence atomic.Uint64

type blockingCloseDriver struct {
	closeStarted chan struct{}
	release      chan struct{}
}

func (d *blockingCloseDriver) Open(string) (driver.Conn, error) {
	return &blockingCloseConn{
		closeStarted: d.closeStarted,
		release:      d.release,
	}, nil
}

type blockingCloseConn struct {
	closeStarted chan struct{}
	release      chan struct{}
	closeOnce    sync.Once
}

func (*blockingCloseConn) Prepare(string) (driver.Stmt, error) {
	return nil, errors.New("prepare is not supported")
}

func (c *blockingCloseConn) Close() error {
	c.closeOnce.Do(func() { close(c.closeStarted) })
	<-c.release
	return nil
}

func (*blockingCloseConn) Begin() (driver.Tx, error) {
	return nil, errors.New("transactions are not supported")
}

func (*blockingCloseConn) Ping(context.Context) error { return nil }

func TestMySQLFixtureLifecycle(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	fixture := NewMySQLFixture()
	t.Cleanup(func() {
		if err := fixture.Teardown(context.Background()); err != nil {
			t.Errorf("cleanup MySQL fixture: %v", err)
		}
	})

	sourceRoot := t.TempDir()
	migrationDir := filepath.Join(sourceRoot, "migration")
	if err := os.Mkdir(migrationDir, 0o755); err != nil {
		t.Fatalf("create migration directory: %v", err)
	}
	copyFixtureSource(t, "testdata/mysql/migration/001_schema.sql", filepath.Join(migrationDir, "001_schema.sql"))
	copyFixtureSource(t, "testdata/mysql/migration/002_quoted_identifier.sql", filepath.Join(migrationDir, "002_quoted_identifier.sql"))
	seedPath := filepath.Join(sourceRoot, "seed.sql")
	copyFixtureSource(t, "testdata/mysql/seed.sql", seedPath)

	spec := FixtureSpec{
		Image:      "mysql:8.4",
		Migrations: migrationDir,
		Seed:       seedPath,
	}
	prepared, err := Prepare(spec)
	if err != nil {
		t.Fatalf("prepare MySQL fixture: %v", err)
	}
	handle, err := fixture.Provision(ctx, prepared)
	if err != nil {
		t.Fatalf("provision MySQL fixture: %v", err)
	}
	descriptor := handle.Connection
	applicationPassword, err := descriptor.Password()
	if err != nil {
		t.Fatalf("read application connection password: %v", err)
	}
	if descriptor.Driver != "mysql" || descriptor.Name != fixtureDatabase || descriptor.Username != fixtureUsername {
		t.Fatalf("application connection descriptor = %v, want mysql fixture application account", descriptor)
	}
	externalDB := openDescriptorDatabase(t, descriptor, descriptor.Username, applicationPassword)
	var externalServerUUID, externalDatabase, externalUser string
	if err := externalDB.QueryRowContext(
		ctx,
		"SELECT @@server_uuid, DATABASE(), CURRENT_USER()",
	).Scan(&externalServerUUID, &externalDatabase, &externalUser); err != nil {
		t.Fatalf("inspect descriptor database: %v", err)
	}
	if externalDatabase != fixtureDatabase || externalUser != fixtureUsername+"@%" {
		t.Fatalf("descriptor selected database/user = %q/%q, want %q/%q", externalDatabase, externalUser, fixtureDatabase, fixtureUsername+"@%")
	}
	if err := externalDB.Close(); err != nil {
		t.Fatalf("close descriptor database: %v", err)
	}
	assertCredentialRejected(t, ctx, descriptor, rootUsername, applicationPassword, "application credential as administrator")

	if _, err := fixture.Provision(ctx, prepared); err == nil || !strings.Contains(err.Error(), "already provisioned") {
		t.Fatalf("provision twice error = %v, want already provisioned", err)
	}

	provisionRows := itemCount(t, ctx, handle)
	if provisionRows != 1 {
		t.Fatalf("provision row count = %d, want 1", provisionRows)
	}

	var serverUUIDBefore string
	if err := handle.SQL.QueryRowContext(ctx, "SELECT @@server_uuid").Scan(&serverUUIDBefore); err != nil {
		t.Fatalf("read server UUID before reset: %v", err)
	}
	if externalServerUUID != serverUUIDBefore {
		t.Fatalf("descriptor server UUID = %q, Go pool server UUID = %q", externalServerUUID, serverUUIDBefore)
	}

	if _, err := handle.SQL.ExecContext(
		ctx,
		"INSERT INTO fixture_item (id, name) VALUES (?, ?)",
		2,
		"mutated",
	); err != nil {
		t.Fatalf("mutate fixture: %v", err)
	}
	if got := itemCount(t, ctx, handle); got != 2 {
		t.Fatalf("mutated row count = %d, want 2", got)
	}
	if err := os.WriteFile(filepath.Join(migrationDir, "001_schema.sql"), []byte("BROKEN EDIT"), 0o644); err != nil {
		t.Fatalf("edit prepared migration source: %v", err)
	}
	if err := os.WriteFile(seedPath, []byte("BROKEN EDIT"), 0o644); err != nil {
		t.Fatalf("edit prepared seed source: %v", err)
	}
	migrationDigest, seedDigest := prepared.MigrationDigest(), prepared.SeedDigest()

	if err := fixture.Reset(ctx); err != nil {
		t.Fatalf("reset MySQL fixture: %v", err)
	}
	if !descriptor.Valid() || handle.Connection != descriptor {
		t.Fatal("successful reset replaced or invalidated the application connection descriptor")
	}
	resetExternalDB := openDescriptorDatabase(t, descriptor, descriptor.Username, applicationPassword)
	if got := itemCountSQL(t, ctx, resetExternalDB); got != 1 {
		t.Fatalf("descriptor reset row count = %d, want 1", got)
	}
	if err := resetExternalDB.Close(); err != nil {
		t.Fatalf("close reset descriptor database: %v", err)
	}

	resetRows := itemCount(t, ctx, handle)
	if resetRows != 1 {
		t.Fatalf("reset row count = %d, want 1", resetRows)
	}
	if prepared.MigrationDigest() != migrationDigest || prepared.SeedDigest() != seedDigest {
		t.Fatal("prepared digests changed after source edits")
	}

	var serverUUIDAfter string
	if err := handle.SQL.QueryRowContext(ctx, "SELECT @@server_uuid").Scan(&serverUUIDAfter); err != nil {
		t.Fatalf("read server UUID after reset: %v", err)
	}
	if serverUUIDAfter != serverUUIDBefore {
		t.Fatalf("server UUID after reset = %q, want %q", serverUUIDAfter, serverUUIDBefore)
	}

	poolBeforeCanceledReset := handle.SQL
	canceledResetCtx, cancelReset := context.WithCancel(context.Background())
	cancelReset()
	if err := fixture.Reset(canceledResetCtx); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled reset error = %v, want %v", err, context.Canceled)
	}
	if handle.SQL != poolBeforeCanceledReset {
		t.Fatal("canceled reset replaced the application database pool")
	}
	if got := itemCount(t, ctx, handle); got != 1 {
		t.Fatalf("row count after canceled reset = %d, want 1", got)
	}
	quarantineCause := errors.New("adapter shutdown did not prove database cleanup")
	fixture.Quarantine(quarantineCause)
	if descriptor.Valid() || handle.Connection.Valid() {
		t.Fatal("quarantine left a fixture connection descriptor valid")
	}
	if password, err := descriptor.Password(); !errors.Is(err, ErrConnectionDescriptorInvalid) || password != "" {
		t.Fatalf("quarantined descriptor password = %q, error = %v", password, err)
	}
	if err := fixture.Ready(handle); !errors.Is(err, ErrQuarantined) || !errors.Is(err, quarantineCause) {
		t.Fatalf("quarantined fixture Ready = %v", err)
	}
	if err := fixture.Reset(ctx); !errors.Is(err, ErrQuarantined) || !errors.Is(err, quarantineCause) {
		t.Fatalf("quarantined fixture Reset = %v", err)
	}

	if err := fixture.Teardown(ctx); err != nil {
		t.Fatalf("teardown MySQL fixture: %v", err)
	}
	if descriptor.Valid() {
		t.Fatal("teardown left the application connection descriptor valid")
	}
	if password, err := descriptor.Password(); !errors.Is(err, ErrConnectionDescriptorInvalid) || password != "" {
		t.Fatalf("torn-down descriptor password = %q, error = %v", password, err)
	}
	badPrepared, err := Prepare(spec)
	if err != nil {
		t.Fatalf("prepare edited SQL for failed reprovision: %v", err)
	}
	if _, err := fixture.Provision(ctx, badPrepared); err == nil || !strings.Contains(err.Error(), "apply SQL") {
		t.Fatalf("bad SQL reprovision error = %v, want SQL application failure", err)
	}
	if err := fixture.Ready(handle); !errors.Is(err, ErrQuarantined) {
		t.Fatalf("failed reprovision cleared quarantine: %v", err)
	}

	reprovisioned, err := fixture.Provision(ctx, prepared)
	if err != nil {
		t.Fatalf("reprovision MySQL fixture: %v", err)
	}
	if err := fixture.Ready(reprovisioned); err != nil {
		t.Fatalf("fresh reprovision still quarantined: %v", err)
	}
	if err := fixture.Ready(handle); !errors.Is(err, ErrConnectionDescriptorInvalid) {
		t.Fatalf("stale handle after reprovision = %v, want invalid descriptor", err)
	}
	if err := fixture.Reset(ctx); err != nil {
		t.Fatalf("reset after reprovision: %v", err)
	}
	freshPassword, err := reprovisioned.Connection.Password()
	if err != nil {
		t.Fatalf("read reprovisioned application password: %v", err)
	}
	if freshPassword == applicationPassword {
		t.Fatal("reprovision reused the stale application credential")
	}
	assertCredentialRejected(
		t,
		ctx,
		reprovisioned.Connection,
		descriptor.Username,
		applicationPassword,
		"stale descriptor after reprovision",
	)
	freshExternalDB := openDescriptorDatabase(t, reprovisioned.Connection, reprovisioned.Connection.Username, freshPassword)
	if got := itemCountSQL(t, ctx, freshExternalDB); got != 1 {
		t.Fatalf("reprovisioned descriptor row count = %d, want 1", got)
	}
	if err := freshExternalDB.Close(); err != nil {
		t.Fatalf("close reprovisioned descriptor database: %v", err)
	}
	if err := fixture.Teardown(ctx); err != nil {
		t.Fatalf("teardown reprovisioned MySQL fixture: %v", err)
	}
	t.Log("FIXTURE_QUARANTINE_RECOVERY_RESULT reset_blocked=true descriptor_invalid=true failed_reprovision=blocked fresh_reprovision=ready stale_handle=blocked")
	if err := fixture.Teardown(ctx); err != nil {
		t.Fatalf("teardown MySQL fixture twice: %v", err)
	}

	t.Logf(
		"FIXTURE_LIFECYCLE_RESULT image=mysql:8.4 provision_rows=%d reset_rows=%d prepared_snapshot=stable digests=stable descriptor_same_database=true descriptor_reset=valid credentials=separate teardown=invalidated stale_reprovision_auth=denied",
		provisionRows,
		resetRows,
	)
}

func openDescriptorDatabase(
	t *testing.T,
	descriptor ConnectionDescriptor,
	username string,
	password string,
) *sql.DB {
	t.Helper()

	db, err := sql.Open("mysql", descriptorDSN(descriptor, username, password))
	if err != nil {
		t.Fatalf("open descriptor database: %v", err)
	}
	if err := db.PingContext(context.Background()); err != nil {
		_ = db.Close()
		t.Fatalf("ping descriptor database: %v", err)
	}
	return db
}

func assertCredentialRejected(
	t *testing.T,
	ctx context.Context,
	descriptor ConnectionDescriptor,
	username string,
	password string,
	name string,
) {
	t.Helper()

	db, err := sql.Open("mysql", descriptorDSN(descriptor, username, password))
	if err != nil {
		t.Fatalf("open %s probe: %v", name, err)
	}
	defer func() {
		if err := db.Close(); err != nil {
			t.Errorf("close %s probe: %v", name, err)
		}
	}()
	if err := db.PingContext(ctx); err == nil {
		t.Fatalf("%s unexpectedly authorized a database connection", name)
	} else if strings.Contains(err.Error(), password) {
		t.Fatalf("%s error disclosed the credential: %v", name, err)
	}
}

func descriptorDSN(descriptor ConnectionDescriptor, username, password string) string {
	config := mysqldriver.NewConfig()
	config.User = username
	config.Passwd = password
	config.Net = "tcp"
	config.Addr = net.JoinHostPort(descriptor.Host, strconv.Itoa(descriptor.Port))
	config.DBName = descriptor.Name
	config.ParseTime = true
	return config.FormatDSN()
}

func itemCountSQL(t *testing.T, ctx context.Context, db *sql.DB) int {
	t.Helper()

	var count int
	if err := db.QueryRowContext(ctx, "SELECT COUNT(*) FROM fixture_item").Scan(&count); err != nil {
		t.Fatalf("count fixture items through descriptor: %v", err)
	}
	return count
}

func copyFixtureSource(t *testing.T, source, destination string) {
	t.Helper()
	content, err := os.ReadFile(source)
	if err != nil {
		t.Fatalf("read fixture source %q: %v", source, err)
	}
	if err := os.WriteFile(destination, content, 0o644); err != nil {
		t.Fatalf("write fixture source %q: %v", destination, err)
	}
}

func TestMySQLFixtureLifecycleErrors(t *testing.T) {
	fixture := NewMySQLFixture()

	if err := fixture.Reset(context.Background()); err == nil || !strings.Contains(err.Error(), "not provisioned") {
		t.Fatalf("reset before provision error = %v, want not provisioned", err)
	}

	if _, err := Prepare(FixtureSpec{}); err == nil || !strings.Contains(err.Error(), "image is required") {
		t.Fatalf("empty image prepare error = %v, want image is required", err)
	}

	_, err := fixture.Provision(context.Background(), Prepared{})
	if err == nil || !strings.Contains(err.Error(), "prepared fixture is required") {
		t.Fatalf("empty prepared fixture error = %v, want prepared fixture is required", err)
	}

	if err := fixture.Teardown(context.Background()); err != nil {
		t.Fatalf("teardown before provision: %v", err)
	}
}

func TestProvisionCleanupUsesIndependentBoundedContext(t *testing.T) {
	operationCtx, cancel := context.WithCancel(context.Background())
	cancel()

	wantErr := errors.New("cleanup failed")
	err := withProvisionCleanupContext(operationCtx, func(cleanupCtx context.Context) error {
		if err := cleanupCtx.Err(); err != nil {
			t.Fatalf("cleanup context error = %v, want active context", err)
		}
		deadline, ok := cleanupCtx.Deadline()
		if !ok {
			t.Fatal("cleanup context has no deadline")
		}
		remaining := time.Until(deadline)
		if remaining <= 0 || remaining > failedProvisionCleanupTimeout {
			t.Fatalf("cleanup context remaining time = %v, want within (0, %v]", remaining, failedProvisionCleanupTimeout)
		}

		return wantErr
	})
	if !errors.Is(err, wantErr) {
		t.Fatalf("cleanup error = %v, want %v", err, wantErr)
	}
}

func TestMySQLFixturePreservesFailedProvisionContainerForTeardown(t *testing.T) {
	wantErr := errors.New("terminate failed")
	terminateCalls := 0
	container := &mysqlcontainer.MySQLContainer{}
	fixture := &mysqlFixture{
		terminateContainer: func(context.Context, *mysqlcontainer.MySQLContainer) error {
			terminateCalls++
			if terminateCalls == 1 {
				return wantErr
			}

			return nil
		},
	}

	err := fixture.cleanupFailedProvision(context.Background(), nil, nil, container)
	if !errors.Is(err, wantErr) {
		t.Fatalf("failed provision cleanup error = %v, want %v", err, wantErr)
	}
	if fixture.container != container {
		t.Fatal("failed provision cleanup discarded the container handle")
	}
	if fixture.provisioned {
		t.Fatal("failed provision cleanup marked the fixture as provisioned")
	}
	if _, err := fixture.Provision(context.Background(), Prepared{}); err == nil || !strings.Contains(err.Error(), "cleanup pending") {
		t.Fatalf("provision during pending cleanup error = %v, want cleanup pending", err)
	}

	if err := fixture.Teardown(context.Background()); err != nil {
		t.Fatalf("retry failed provision cleanup: %v", err)
	}
	if fixture.container != nil {
		t.Fatal("successful teardown retained the failed provision container")
	}
	if terminateCalls != 2 {
		t.Fatalf("terminate calls = %d, want 2", terminateCalls)
	}
}

func TestMySQLFixtureTeardownRetriesFailedTermination(t *testing.T) {
	wantErr := errors.New("terminate failed")
	quarantineCause := errors.New("adapter Stop failed")
	const adminPassword = "admin-retry-secret"
	const applicationPassword = "application-retry-secret"
	terminateCalls := 0
	fixture := &mysqlFixture{
		container: &mysqlcontainer.MySQLContainer{},
		db: &DB{Connection: newConnectionDescriptor(
			"mysql", "127.0.0.1", 3306, fixtureDatabase, fixtureUsername, applicationPassword,
		)},
		prepared:            Prepared{image: "mysql:8.4", valid: true},
		provisioned:         true,
		adminPassword:       adminPassword,
		applicationPassword: applicationPassword,
		terminateContainer: func(context.Context, *mysqlcontainer.MySQLContainer) error {
			terminateCalls++
			if terminateCalls == 1 {
				return fmt.Errorf("%w: %s %s", wantErr, adminPassword, applicationPassword)
			}

			return nil
		},
	}
	fixture.Quarantine(quarantineCause)

	if err := fixture.Teardown(context.Background()); !errors.Is(err, wantErr) {
		t.Fatalf("first teardown error = %v, want %v", err, wantErr)
	} else if strings.Contains(err.Error(), adminPassword) || strings.Contains(err.Error(), applicationPassword) {
		t.Fatalf("first teardown error disclosed a credential: %v", err)
	}
	if fixture.container == nil || !fixture.provisioned {
		t.Fatal("failed teardown discarded retryable fixture state")
	}
	if fixture.db.Connection.Valid() {
		t.Fatal("failed teardown left the connection descriptor valid")
	}
	if err := fixture.Ready(fixture.db); !errors.Is(err, ErrQuarantined) || !errors.Is(err, quarantineCause) {
		t.Fatalf("failed teardown lost quarantine: %v", err)
	}
	if _, err := fixture.Provision(context.Background(), Prepared{}); err == nil || !strings.Contains(err.Error(), "already provisioned") {
		t.Fatalf("provision during pending cleanup error = %v, want already provisioned", err)
	}

	if err := fixture.Teardown(context.Background()); err != nil {
		t.Fatalf("retry teardown: %v", err)
	}
	if fixture.container != nil || fixture.provisioned {
		t.Fatal("successful teardown retained fixture state")
	}
	if err := fixture.Ready(nil); !errors.Is(err, ErrQuarantined) || !errors.Is(err, quarantineCause) {
		t.Fatalf("teardown cleared quarantine before reprovision: %v", err)
	}
	if fixture.adminPassword != "" || fixture.applicationPassword != "" {
		t.Fatal("successful teardown retained fixture credentials")
	}
	if err := fixture.Teardown(context.Background()); err != nil {
		t.Fatalf("teardown after cleanup: %v", err)
	}
	if terminateCalls != 2 {
		t.Fatalf("terminate calls = %d, want 2", terminateCalls)
	}
}

func TestMySQLFixtureTeardownHonorsContextDeadline(t *testing.T) {
	terminateCalls := 0
	container := &mysqlcontainer.MySQLContainer{}
	fixture := &mysqlFixture{
		container: container,
		db: &DB{Connection: newConnectionDescriptor(
			"mysql", "127.0.0.1", 3306, fixtureDatabase, fixtureUsername, "deadline-secret",
		)},
		prepared:    Prepared{image: "mysql:8.4", valid: true},
		provisioned: true,
		terminateContainer: func(ctx context.Context, got *mysqlcontainer.MySQLContainer) error {
			terminateCalls++
			if got != container {
				t.Fatalf("terminate container = %p, want %p", got, container)
			}
			<-ctx.Done()
			return ctx.Err()
		},
	}

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	started := time.Now()
	err := fixture.Teardown(ctx)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("teardown error = %v, want %v", err, context.DeadlineExceeded)
	}
	if elapsed := time.Since(started); elapsed > time.Second {
		t.Fatalf("teardown elapsed = %v, want bounded by context deadline", elapsed)
	}
	if terminateCalls != 1 {
		t.Fatalf("terminate calls = %d, want 1", terminateCalls)
	}
	if fixture.container != container || !fixture.provisioned {
		t.Fatal("deadline-exceeded teardown discarded retryable fixture state")
	}
	if fixture.db.Connection.Valid() {
		t.Fatal("deadline-exceeded teardown left the connection descriptor valid")
	}

	assertMySQLFixtureBoundsPoolClose(t)

	t.Log("FIXTURE_TEARDOWN_CONTEXT_RESULT deadline=honored calls=1 state=retryable pool_close=bounded")
}

func assertMySQLFixtureBoundsPoolClose(t *testing.T) {
	t.Helper()

	closeStarted := make(chan struct{})
	release := make(chan struct{})
	t.Cleanup(func() { close(release) })
	driverName := fmt.Sprintf("weavegate-blocking-close-%d", blockingCloseDriverSequence.Add(1))
	sql.Register(driverName, &blockingCloseDriver{
		closeStarted: closeStarted,
		release:      release,
	})
	db, err := sql.Open(driverName, "")
	if err != nil {
		t.Fatalf("open blocking-close database: %v", err)
	}
	if err := db.PingContext(context.Background()); err != nil {
		t.Fatalf("ping blocking-close database: %v", err)
	}

	terminateCalls := 0
	fixture := &mysqlFixture{
		container:   &mysqlcontainer.MySQLContainer{},
		db:          &DB{SQL: db},
		prepared:    Prepared{image: "mysql:8.4", valid: true},
		provisioned: true,
		terminateContainer: func(context.Context, *mysqlcontainer.MySQLContainer) error {
			terminateCalls++
			return nil
		},
	}
	fixture.Quarantine(errors.New("adapter Stop failed"))

	started := time.Now()
	err = fixture.Teardown(context.Background())
	elapsed := time.Since(started)
	if err == nil || !strings.Contains(err.Error(), "exceeded 5s teardown budget") {
		t.Fatalf("teardown error = %v, want pool-close budget error", err)
	}
	if elapsed < poolCloseTimeout || elapsed > poolCloseTimeout+time.Second {
		t.Fatalf("teardown elapsed = %v, want within [%v, %v]", elapsed, poolCloseTimeout, poolCloseTimeout+time.Second)
	}
	select {
	case <-closeStarted:
	default:
		t.Fatal("database driver Close was not called")
	}
	if terminateCalls != 1 {
		t.Fatalf("terminate calls = %d, want 1 after bounded pool close", terminateCalls)
	}
	if fixture.container != nil || fixture.provisioned {
		t.Fatal("successful termination retained fixture state after pool-close timeout")
	}
	if _, err := fixture.Provision(context.Background(), Prepared{}); !errors.Is(err, ErrQuarantined) {
		t.Fatalf("provision after failed teardown = %v, want quarantine", err)
	}
	if err := fixture.Teardown(context.Background()); err != nil {
		t.Fatalf("retry teardown after container termination: %v", err)
	}
}

func itemCount(t *testing.T, ctx context.Context, db *DB) int {
	t.Helper()

	var count int
	if err := db.SQL.QueryRowContext(ctx, "SELECT COUNT(*) FROM fixture_item").Scan(&count); err != nil {
		t.Fatalf("count fixture items: %v", err)
	}

	return count
}
