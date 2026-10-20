# Commands used to prepare the local campaign

These commands are input preparation, outside the measured gate. Run the Git
commands from a full weavegate checkout. The local paths are part of the
retained invocation identity; a new machine uses its own new registration.
No gate was run during preparation.

```bash
mkdir -p /tmp/weavegate-a1
gh release download v0.2.0 --repo weavegate/weavegate \
  --dir /tmp/weavegate-a1 --pattern '*linux_amd64*' --pattern '*checksums*'
git clone https://github.com/weavegate/spring-boot-adoption-example.git \
  /tmp/weavegate-a1/example
mkdir -p /tmp/weavegate-a1/baseline /tmp/weavegate-a1/bplus \
  /tmp/weavegate-a1/fixed /tmp/weavegate-a1/vulnerable
(cd /tmp/weavegate-a1 && sha256sum -c checksums.txt --ignore-missing)
tar -xzf /tmp/weavegate-a1/weavegate_0.2.0_linux_amd64.tar.gz \
  -C /tmp/weavegate-a1/baseline
git archive ee10c7de2114086f14258037703dbd4b104e5908 | \
  tar -x -C /tmp/weavegate-a1/bplus
git diff 8d4bf35^ 8d4bf35 -- cmd internal > /tmp/weavegate-a1/instrumentation.patch
git -C /tmp/weavegate-a1/bplus apply /tmp/weavegate-a1/instrumentation.patch
(cd /tmp/weavegate-a1/bplus && CGO_ENABLED=0 go build -trimpath=false \
  -ldflags '-s -w -X main.version=0.2.0+timing' \
  -o /tmp/weavegate-a1/weavegate-bplus ./cmd/weavegate)
git -C /tmp/weavegate-a1/example archive f38b20b1634235a0f46c96a2238ba10e2143fa75 | \
  tar -x -C /tmp/weavegate-a1/fixed
git -C /tmp/weavegate-a1/example archive e4932192b160e60c967dc17c9d02ac478bd61968 | \
  tar -x -C /tmp/weavegate-a1/vulnerable
(cd /tmp/weavegate-a1/fixed && ./mvnw -B \
  -Dmaven.repo.local=/tmp/weavegate-a1/m2 -DskipTests package dependency:tree \
  > /tmp/weavegate-a1/fixed-build.log 2>&1)
(cd /tmp/weavegate-a1/vulnerable && ./mvnw -B \
  -Dmaven.repo.local=/tmp/weavegate-a1/m2 -DskipTests package dependency:tree \
  > /tmp/weavegate-a1/vulnerable-build.log 2>&1)
```

MySQL was already present locally. `freeze.py` records its resolved digest and
puts that digest in both configurations. To prepare the same image elsewhere:

```bash
docker pull mysql@sha256:8dbcf531a03aade657e181b9cf2f1d1803ce621a1d55610cb44cb531ab7d7db6
```

The initial build without release linker flags was replaced before freeze;
only the final binary is measured. Instrumentation checks used:

```bash
(cd /tmp/weavegate-a1/bplus && go test ./internal/timing ./internal/report -count=20)
(cd /tmp/weavegate-a1/bplus && go test ./internal/orchestrator -run '^TestTiming' -count=20)
```
