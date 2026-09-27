// Package fixture provisions and resets database fixtures used by weavegate.
package fixture

import (
	"context"
	"database/sql"
	"errors"
)

// ErrQuarantined means cleanup was not proven. Reset is forbidden until
// teardown and a successful new provision establish a fresh database.
var ErrQuarantined = errors.New("fixture is quarantined")

// Fixture manages the runtime lifecycle used by the orchestrator after an
// isolated database fixture has been provisioned.
type Fixture interface {
	// Ready rejects a quarantined fixture or a stale database handle without
	// changing state. Reset repeats its own checks before touching the database.
	Ready(*DB) error
	Reset(context.Context) error
	// Quarantine latches an uncertain adapter shutdown or session fault and
	// invalidates any connection descriptor before another Reset can begin.
	Quarantine(error)
	// Teardown must honor cancellation and deadlines on the supplied context.
	// Callers provide a cleanup-specific context independent of the operation.
	Teardown(context.Context) error
}

// Provisioner owns the external side effect that creates a prepared fixture.
// Keeping it separate from Fixture prevents the orchestrator from acquiring a
// provisioning capability it never uses.
type Provisioner interface {
	Fixture
	Provision(context.Context, Prepared) (*DB, error)
}

// FixtureSpec describes the database image and SQL sources for a fixture.
type FixtureSpec struct {
	Image      string
	Migrations string
	Seed       string
}

// Prepared is an immutable snapshot of the fixture sources. Its fields are
// intentionally private: Provision and Reset apply only the bytes parsed by
// Prepare, while evidence reads the digests derived from the same snapshot.
type Prepared struct {
	image           string
	migrations      []preparedSQLSource
	seed            preparedSQLSource
	migrationDigest string
	seedDigest      string
	valid           bool
}

// Image returns the database container image captured during preparation.
func (p Prepared) Image() string { return p.image }

// MigrationDigest returns the digest derived from the prepared migrations.
func (p Prepared) MigrationDigest() string { return p.migrationDigest }

// SeedDigest returns the digest derived from the prepared seed bytes.
func (p Prepared) SeedDigest() string { return p.seedDigest }

// DB exposes the fixture's managed database connection pool and the structured
// application connection descriptor for that same prepared database.
//
// A reset may replace SQL while preserving the DB wrapper and Connection.
// Callers must not retain the pool or use the fixture concurrently with Reset.
type DB struct {
	SQL        *sql.DB
	Connection ConnectionDescriptor
}
