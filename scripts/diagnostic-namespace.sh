#!/usr/bin/env bash

set -euo pipefail

# This script contains the legacy-prefix patterns and self-test fixtures, so it
# is the only tracked text file excluded from its own repository scan.
readonly script_path='scripts/diagnostic-namespace.sh'
readonly legacy_pattern='[Rr][Gg][0-9]{3}|\bRG\b'
self_test_root=''

scan_repository() {
  local requested_root=$1
  local repository_root
  local relative_path
  local absolute_path
  local link_target
  local grep_status
  local found=0

  repository_root=$(git -C "$requested_root" rev-parse --show-toplevel)

  while IFS= read -r -d '' relative_path; do
    if [[ "$relative_path" == "$script_path" ]]; then
      continue
    fi

    absolute_path="$repository_root/$relative_path"
    if [[ -L "$absolute_path" ]]; then
      link_target=$(readlink -- "$absolute_path")
      if printf '%s\n' "$link_target" | grep -nE "$legacy_pattern" > /dev/null; then
        printf '%s:1:%s\n' "$relative_path" "$link_target"
        found=1
      fi
      continue
    fi

    # Compressed evidence and other binary bytes are not diagnostic text.
    if grep -InHE "$legacy_pattern" -- "$absolute_path"; then
      found=1
    else
      grep_status=$?
      if [[ $grep_status -ne 1 ]]; then
        return "$grep_status"
      fi
    fi
  done < <(git -C "$repository_root" ls-files -z)

  if [[ $found -ne 0 ]]; then
    return 1
  fi
  printf 'DIAGNOSTIC_NAMESPACE_RESULT prefix=WG legacy=rejected scope=tracked_files\n'
}

run_self_test() {
  self_test_root=$(mktemp -d)
  trap 'rm -rf -- "${self_test_root:?}"' EXIT
  git -C "$self_test_root" init -q

  printf 'error[RG001]\n' > "$self_test_root/fixture.txt"
  git -C "$self_test_root" add fixture.txt
  if scan_repository "$self_test_root" > /dev/null; then
    echo 'diagnostic namespace guard accepted a legacy diagnostic code' >&2
    return 1
  fi
  echo 'GATE_BITES'

  printf 'the RG code\n' > "$self_test_root/fixture.txt"
  git -C "$self_test_root" add fixture.txt
  if scan_repository "$self_test_root" > /dev/null; then
    echo 'diagnostic namespace guard accepted a bare legacy prefix' >&2
    return 1
  fi
  echo 'BARE_RG_CAUGHT'

  printf 'flaky=rg090\n' > "$self_test_root/fixture.txt"
  git -C "$self_test_root" add fixture.txt
  if scan_repository "$self_test_root" > /dev/null; then
    echo 'diagnostic namespace guard accepted a lowercase legacy diagnostic code' >&2
    return 1
  fi
  echo 'LOWER_RG_CAUGHT'

  printf 'diagnostic=WG001\n' > "$self_test_root/fixture.txt"
  git -C "$self_test_root" add fixture.txt
  if ! scan_repository "$self_test_root" > /dev/null; then
    echo 'diagnostic namespace guard rejected a clean fixture' >&2
    return 1
  fi

  printf '\0RG001\0RG\0rg090\n' > "$self_test_root/evidence.bin"
  git -C "$self_test_root" add evidence.bin
  if ! scan_repository "$self_test_root" > /dev/null; then
    echo 'diagnostic namespace guard treated binary bytes as diagnostic text' >&2
    return 1
  fi
  echo 'BINARY_IGNORED'

  printf 'error[RG001]\n' > "$self_test_root/evidence.tar.gz"
  git -C "$self_test_root" add evidence.tar.gz
  if scan_repository "$self_test_root" > /dev/null; then
    echo 'diagnostic namespace guard skipped text with an archive extension' >&2
    return 1
  fi
  echo 'ARCHIVE_NAMED_TEXT_CAUGHT'
}

if [[ ${1:-} == '--self-test' ]]; then
  if [[ $# -ne 1 ]]; then
    printf 'usage: %s --self-test | [root]\n' "$0" >&2
    exit 1
  fi
  run_self_test
  exit 0
fi

if [[ $# -gt 1 ]]; then
  printf 'usage: %s --self-test | [root]\n' "$0" >&2
  exit 1
fi

scan_repository "${1:-.}"
