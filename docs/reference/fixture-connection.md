# Fixture application connection descriptor

`fixture.DB.Connection` is the fixture-owned description of the application
account for the same prepared database exposed through `fixture.DB.SQL`. It is
an engine extension boundary for adapters that cannot consume a Go SQL pool.
The [external adapter](config.md#external-jvm) consumes it at startup.

## Shape and access

`ConnectionDescriptor` exposes structured `Driver`, `Host`, `Port`, `Name`,
and `Username` metadata. `Driver` is currently `mysql`; `Port` is the
host-mapped port in the range 1–65535. The descriptor is not a Go DSN or JDBC
URL, and neither the administrator DSN nor driver internals are inspected to
construct it.

The password is deliberately absent from exported fields. `Password()` returns
it only while the descriptor is valid, and returns
`ErrConnectionDescriptorInvalid` with an empty password after invalidation.
Generic Go formatting, including `%#v`, emits `<redacted>`, and generic JSON
serialization can include only the non-secret exported metadata.

An adapter owns the password from the moment `Password()` returns until its
single private transport write completes. For the proposed external SUT v1
protocol, the adapter must construct the `database` object and write it only to
the child process's owned stdin. It must not place the descriptor or password
in argv, environment variables, temporary files, errors, stderr/stdout logs,
normalized traces, or report artifacts. Raw start frames are not persisted.
Fixture provisioning sanitizes both application and administrator credentials
from its returned errors; future consumers have the same obligation for errors
created after password access.

## Ownership and lifecycle

The prepared fixture owns both the descriptor's shared validity state and its
credential. Copying a descriptor does not extend its lifetime: all copies
observe the same invalidation.

| Lifecycle event | Descriptor contract |
| --- | --- |
| Successful `Provision` | Returns one valid descriptor for the prepared database and its database-scoped application account. Administrator access uses a separate credential and is never exposed in `fixture.DB`. |
| Successful `Reset` | Preserves the descriptor, endpoint, account, and credential while replacing the application pool and reapplying the same prepared migrations and seed. External clients must already be stopped and their connections closed before Reset. |
| Reset failure after reset work starts | Invalidates the descriptor. The fixture must be torn down rather than reused. Cancellation detected before pool closure leaves it valid because no reset work started. |
| `Teardown` begins cleanup | Invalidates the descriptor and erases its internally held password before pool/container cleanup. Invalidation remains final even if cleanup must be retried. A call rejected for an already-canceled context performs no cleanup and does not change validity. |
| Successful reprovisioning | Creates a new descriptor with fresh application and administrator credentials. A stale descriptor cannot reveal a password or authorize the new fixture, even if the host reuses a mapped port. |

MySQL provisioning creates one database and two access paths: the private root
administrator pool owns database creation/reset, while the application account
is granted access only to the prepared `weavegate` database. The Go pool and
descriptor both use that application account and endpoint. No second database
or independent provisioner exists.

The Docker-backed fixture lifecycle test compares `@@server_uuid`, selected
database, application identity, and seeded rows through both access paths. It
also verifies Reset preservation, administrator rejection of the application
credential, teardown invalidation, and rejection of stale credentials after
reprovisioning.
