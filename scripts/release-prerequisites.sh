#!/usr/bin/env bash

set -euo pipefail

# Repository secrets the Java publication job needs. A tag run checks them
# before any job publishes, so a missing one cannot leave a CLI release without
# its matching Java version.
required_secrets=(
  MAVEN_CENTRAL_GPG_PRIVATE_KEY_B64
  MAVEN_CENTRAL_GPG_PASSPHRASE
  MAVEN_CENTRAL_USERNAME
  MAVEN_CENTRAL_PASSWORD
)

gate_job=prerequisites

usage() {
  printf 'usage: %s check | gated <workflow-path> | --self-test\n' "$0" >&2
}

# check reads each required secret from the environment variable of the same
# name and reports missing names only; values are never printed.
check() {
  if [[ $# -ne 0 ]]; then
    usage
    return 1
  fi

  local name
  local missing=()
  for name in "${required_secrets[@]}"; do
    if [[ -z ${!name:-} ]]; then
      missing+=("$name")
    fi
  done

  if [[ ${#missing[@]} -gt 0 ]]; then
    for name in "${missing[@]}"; do
      printf 'release prerequisites: missing repository secret %s\n' "$name" >&2
    done
    printf 'release prerequisites: stopping before publication; nothing was published\n' >&2
    return 1
  fi
  printf 'release prerequisites: all %d Java publication secrets are present\n' "${#required_secrets[@]}"
}

# gated verifies that every other job in the workflow needs the gate job,
# directly or through another job, so no job can publish before it passes.
gated() {
  if [[ $# -ne 1 ]]; then
    usage
    return 1
  fi

  local workflow_path=$1
  if [[ ! -f $workflow_path ]]; then
    printf 'release prerequisites: workflow does not exist: %s\n' "$workflow_path" >&2
    return 1
  fi

  awk -v gate="$gate_job" -v workflow_path="$workflow_path" '
    /^[^[:space:]#]/ { in_jobs = ($0 ~ /^jobs:[[:space:]]*$/); job = ""; next }
    !in_jobs { next }
    /^  [A-Za-z0-9_-]+:[[:space:]]*$/ {
      job = $0
      sub(/^  /, "", job)
      sub(/:.*/, "", job)
      jobs[++job_count] = job
      next
    }
    job != "" && /^    needs:/ {
      value = $0
      sub(/^    needs:[[:space:]]*/, "", value)
      gsub(/[][",[:space:]'\'']+/, " ", value)
      needs[job] = value
    }
    END {
      if (!(gate in needs) && !has_job(gate)) {
        printf "%s: has no %s job\n", workflow_path, gate > "/dev/stderr"
        exit 1
      }
      if (needs[gate] != "") {
        printf "%s: %s job must not depend on another job\n", workflow_path, gate > "/dev/stderr"
        exit 1
      }
      failed = 0
      for (i = 1; i <= job_count; i++) {
        if (jobs[i] != gate && !reaches(jobs[i], 0)) {
          printf "%s: job %s can run before %s\n", workflow_path, jobs[i], gate > "/dev/stderr"
          failed = 1
        }
      }
      exit failed
    }
    function has_job(name,    k) {
      for (k = 1; k <= job_count; k++) {
        if (jobs[k] == name) {
          return 1
        }
      }
      return 0
    }
    function reaches(name, depth,    parts, n, k) {
      if (name == gate) {
        return 1
      }
      if (depth > job_count) {
        return 0
      }
      n = split(needs[name], parts, " ")
      for (k = 1; k <= n; k++) {
        if (parts[k] != "" && reaches(parts[k], depth + 1)) {
          return 1
        }
      }
      return 0
    }
  ' "$workflow_path"
}

self_test() {
  release_prerequisites_test_root=$(mktemp -d)
  trap 'rm -rf "$release_prerequisites_test_root"' EXIT

  local name
  local sentinel
  for name in "${required_secrets[@]}"; do
    export "$name=sentinel-value-$name"
  done
  check > /dev/null
  printf 'COMPLETE_SECRETS_ACCEPTED\n'

  local output="$release_prerequisites_test_root/check.out"
  local missing
  for missing in "${required_secrets[@]}"; do
    unset "$missing"
    if check > "$output" 2>&1; then
      printf 'release prerequisites self-test: accepted missing %s\n' "$missing" >&2
      return 1
    fi
    if ! grep -qxF "release prerequisites: missing repository secret $missing" "$output"; then
      printf 'release prerequisites self-test: did not name missing %s\n' "$missing" >&2
      return 1
    fi
    for name in "${required_secrets[@]}"; do
      sentinel="sentinel-value-$name"
      if grep -qF "$sentinel" "$output"; then
        printf 'release prerequisites self-test: revealed the value of %s\n' "$name" >&2
        return 1
      fi
    done
    export "$missing=sentinel-value-$missing"
  done
  printf 'EACH_MISSING_SECRET_CAUGHT\n'
  printf 'SECRET_VALUES_HIDDEN\n'

  for name in "${required_secrets[@]}"; do
    unset "$name"
  done
  if check > "$output" 2>&1; then
    printf 'release prerequisites self-test: accepted no secrets\n' >&2
    return 1
  fi
  if [[ $(grep -c '^release prerequisites: missing repository secret ' "$output") -ne ${#required_secrets[@]} ]]; then
    printf 'release prerequisites self-test: did not name every missing secret\n' >&2
    return 1
  fi
  printf 'ALL_MISSING_SECRETS_NAMED\n'

  cat > "$release_prerequisites_test_root/gated.yml" <<'WORKFLOW'
name: release
"on":
  push:
    tags:
      - "v*"
jobs:
  prerequisites:
    runs-on: ubuntu-latest
  release:
    needs: prerequisites
    runs-on: ubuntu-latest
  publish-java:
    needs: [release]
    runs-on: ubuntu-latest
WORKFLOW
  gated "$release_prerequisites_test_root/gated.yml"
  printf 'GATED_WORKFLOW_ACCEPTED\n'

  sed '/^    needs: prerequisites$/d' "$release_prerequisites_test_root/gated.yml" \
    > "$release_prerequisites_test_root/ungated.yml"
  if gated "$release_prerequisites_test_root/ungated.yml" 2> /dev/null; then
    printf 'release prerequisites self-test: accepted a job that runs before the gate\n' >&2
    return 1
  fi
  printf 'UNGATED_JOB_CAUGHT\n'

  sed 's/^  prerequisites:$/  preflight:/' "$release_prerequisites_test_root/gated.yml" \
    > "$release_prerequisites_test_root/no-gate.yml"
  if gated "$release_prerequisites_test_root/no-gate.yml" 2> /dev/null; then
    printf 'release prerequisites self-test: accepted a workflow without the gate job\n' >&2
    return 1
  fi
  printf 'MISSING_GATE_CAUGHT\n'

  printf '%s\n' 'RELEASE_PREREQUISITES_RESULT complete=accepted each_missing=rejected all_missing=named values=hidden ungated_job=rejected missing_gate=rejected'
}

if [[ ${1:-} == '--self-test' ]]; then
  if [[ $# -ne 1 ]]; then
    usage
    exit 1
  fi
  self_test
  exit
fi

case ${1:-} in
  check)
    shift
    check "$@"
    exit
    ;;
  gated)
    shift
    gated "$@"
    exit
    ;;
esac

usage
exit 1
