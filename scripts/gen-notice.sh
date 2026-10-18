#!/usr/bin/env bash

set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT

# Tool installation can fail on transient module proxy/checksum transport errors.
# Retry the same pinned, checksum-verified install; persistent failures stay fatal.
for attempt in 1 2 3; do
  if GOBIN="$work_dir/bin" go install github.com/google/go-licenses@v1.6.0; then
    break
  else
    install_status=$?
    if [[ "$attempt" == 3 ]]; then
      printf 'go-licenses installation failed after 3 attempts\n' >&2
      exit "$install_status"
    fi
    printf 'Retrying go-licenses installation after attempt %s\n' "$attempt" >&2
  fi
done
license_tool="$work_dir/bin/go-licenses"
targets=(linux/amd64 linux/arm64 darwin/amd64 darwin/arm64)

for target in "${targets[@]}"; do
  goos=${target%/*}
  goarch=${target#*/}
  ignore_args=(--ignore=github.com/weavegate/weavegate)

  while IFS= read -r package; do
    ignore_args+=("--ignore=$package")
  done < <(GOOS="$goos" GOARCH="$goarch" go list std)

  GOOS="$goos" GOARCH="$goarch" "$license_tool" report \
    --template "$repo_root/.go-licenses-notice.tpl" \
    "${ignore_args[@]}" ./cmd/weavegate > "$work_dir/$goos-$goarch.tsv"
done

grep -h -v '^$' "$work_dir"/*.tsv | sort -u > "$work_dir/dependencies.tsv"

{
  printf '%s\n' \
    'weavegate' \
    'Copyright 2026 weavegate contributors' \
    '' \
    'This product includes the union of third-party runtime dependencies for' \
    'linux/amd64, linux/arm64, darwin/amd64, and darwin/arm64. Regenerate this' \
    'inventory from the repository root with:' \
    '' \
    './scripts/gen-notice.sh' \
    '' \
    'Go standard library packages and the weavegate module itself are excluded.' \
    '' \
    'Package,License,License source'
  while IFS=$'\t' read -r name version spdx url license_path; do
    printf '%s,%s,%s\n' "$name" "$spdx" "$url"
  done < "$work_dir/dependencies.tsv"

  printf '\nFull license texts\n'
  while IFS=$'\t' read -r name version spdx url license_path; do
    printf '\n------------------------------------------------------------------------\n'
    printf 'Package: %s\n' "$name"
    printf 'Version: %s\n' "$version"
    printf 'License: %s\n' "$spdx"
    printf 'Upstream: %s\n' "$url"
    if [[ "$name" == github.com/go-sql-driver/mysql ]]; then
      printf 'The corresponding MPL-2.0 source code is available from https://github.com/go-sql-driver/mysql.\n'
    fi
    printf '\n'
    awk '
      {
        sub(/[[:space:]]+$/, "")
        lines[NR] = $0
      }
      END {
        last = NR
        while (last > 0 && lines[last] == "") {
          last--
        }
        for (line = 1; line <= last; line++) {
          print lines[line]
        }
      }
    ' "$license_path"
  done < "$work_dir/dependencies.tsv"
} > "$repo_root/NOTICE"
