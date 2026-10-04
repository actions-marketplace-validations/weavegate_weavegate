# Publish the Java Spring integration

The `sdk/java` artifact is configured for Maven Central as
`io.github.weavegate:weavegate-spring:<version>`. The release workflow derives
`<version>` from the exact `v<version>` tag that also publishes the CLI. It
builds and verifies a signed Central bundle before uploading it; branches and
pull requests run only the non-publishing bundle check in the smoke workflow.
The ordinary development build and isolated Java acceptance job use no release
profile and retain their existing behavior. This describes the publication
path; no version is available until a tagged run successfully publishes it.

## One-time prerequisites

1. Verify ownership of the `io.github.weavegate` namespace in the
   [Central Publisher Portal](https://central.sonatype.org/register/central-portal/).
   Confirm the account can publish `weavegate-spring` under that namespace.
2. Create a dedicated GPG signing key, publish its public key as required by
   [Central's signing instructions](https://central.sonatype.org/publish/requirements/gpg/),
   and generate a Central Portal user token. Store these as GitHub Actions
   repository secrets: `MAVEN_CENTRAL_GPG_PRIVATE_KEY_B64` (base64-encoded
   private-key export), `MAVEN_CENTRAL_GPG_PASSPHRASE`,
   `MAVEN_CENTRAL_USERNAME`, and `MAVEN_CENTRAL_PASSWORD` (the token pair).
   Never commit or upload the key or token.
3. Before the first tag, review the POM metadata, transitive dependencies,
   source and Javadoc JARs, and license attribution. Maven Central versions are
   immutable: a bad release needs a new version.

## Before each release tag

Run the repository [release checklist](../../CONTRIBUTING.md#releasing). Check
that the `java-central-bundle` smoke job passed for the tagged commit. It
uses an ephemeral signing key and no Central credentials. To reproduce the
bundle check locally without uploading, use an isolated GPG home containing a
throwaway key and a clean checkout:

```bash
export GNUPGHOME="$(mktemp -d)"
chmod 700 "$GNUPGHOME"
gpg --batch --pinentry-mode loopback --passphrase '' \
  --quick-generate-key 'weavegate local test <test@weavegate.invalid>' default default never
python3 scripts/java-central.py set-version 0.2.0-rc.1
(cd sdk/java && ./mvnw -B -Pcentral-release -Dmaven.test.skip=true clean verify)
python3 scripts/java-central.py build-bundle 0.2.0-rc.1
python3 scripts/java-central.py verify 0.2.0-rc.1
```

Replace the example version with the candidate tag's version. Run this in a
disposable checkout because `set-version` edits `sdk/java/pom.xml`. Remove the
temporary GPG home afterward. The verifier checks the four artifacts, signatures,
checksums, coordinates, metadata, and contents. The fixed
`JAVA_CENTRAL_BUNDLE_RESULT` marker is asserted with `grep -F` in CI. The
non-publishing build makes no portal request and needs no publishing secrets.

## After a prerelease tag

The release job first publishes the CLI assets. The dependent Java job imports
the signing key, builds the complete artifact set without uploading, and verifies
the bundle. The publish command verifies the same ZIP bytes again, uploads those
bytes through the [Central Publisher API](https://central.sonatype.org/publish/publish-portal-api/)
with automatic publication, and waits for Central to report the expected
coordinate as published. It does not rerun Maven after verification. The signing
key is imported into a temporary GPG home, and the token is read from workflow
secrets into process memory; neither is committed. The bundle is not uploaded
as a GitHub artifact.

On a job retry, the publisher first checks whether the same POM is already
available on Maven Central with a valid signature. Otherwise it looks up the
deployment named for the tag version and release commit in the Publisher Portal
and resumes polling that deployment. It uploads a new bundle only when neither
check finds the release. A failed or ambiguous existing deployment stops the
job for investigation instead of creating another deployment for the immutable
coordinate. Java publication jobs for the same tag run sequentially.

From scratch projects outside this repository, use default Maven and Gradle
repositories to resolve the published coordinate and compile a class importing
`io.github.weavegate.sdk.Weavegate`. Use the dependency declarations in the
[Java peer reference](../reference/external-sut-java.md#supported-baseline).
Compare the resolved dependency version to the CLI release version from the same
tag. Save the two build commands and their output with the release evidence.
Do this after each prerelease candidate before treating the publication path as
accepted; the local bundle check alone cannot prove public resolution.
