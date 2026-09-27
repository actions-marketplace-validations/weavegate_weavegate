# Install weavegate

The matching-slice example needs a running Docker daemon to start MySQL 8.4.
Choose one installation route below, then follow the [Quickstart](quickstart.md)
from the directory containing `fixtures/matching-slice/.weavegate/config.yaml`.

## From a release archive

Download the archive for your operating system and architecture from
[GitHub Releases](https://github.com/weavegate/weavegate/releases). Extract one
archive and run these commands from the directory where you downloaded it:

```bash
tar -xzf weavegate_*.tar.gz
cd weavegate_*/
export PATH="$PWD:$PATH"
weavegate --version
```

The archive contains the CLI binary and the matching-slice configuration,
migration, and seed files. Stay inside the extracted directory when running the
Quickstart commands; no source checkout or Go toolchain is needed.

## From source

Install Go 1.25 or newer, then build from a checkout:

```bash
git clone https://github.com/weavegate/weavegate.git
cd weavegate
go build -o weavegate ./cmd/weavegate
export PATH="$PWD:$PATH"
weavegate --version
```

The matching-slice fixture files are in this checkout. Run the Quickstart
commands from its root.

## With go install

Install a published module version with Go 1.25 or newer:

```bash
go install github.com/weavegate/weavegate/cmd/weavegate@latest
export PATH="$(go env GOPATH)/bin:$PATH"
weavegate --version
```

If `GOBIN` is set, add that directory to `PATH` instead. Installed builds from
versions with module-version reporting show their resolved module version with
`--version`.

`go install` installs only the binary. The example still needs the matching-slice
configuration, migration, and seed files. Get them from a source checkout or a
release archive, change into that directory, and run the [Quickstart](quickstart.md)
commands there.
