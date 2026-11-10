#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
: "${SOURCE_DATE_EPOCH:?set SOURCE_DATE_EPOCH to the release source timestamp}"
case "$SOURCE_DATE_EPOCH" in (*[!0-9]*) exit 2;; esac
version=$(sed -n 's/^project(flowcore VERSION \([^ ]*\) .*/\1/p' CMakeLists.txt)
test -n "$version"
epoch=$(git log -1 --format=%ct)
if [[ "$SOURCE_DATE_EPOCH" != "$epoch" ]]; then
  echo 'SOURCE_DATE_EPOCH must match the archived Git commit timestamp' >&2
  exit 2
fi
if ! git diff --quiet HEAD --; then
  echo 'Source archive requires a clean tracked checkout' >&2
  exit 2
fi
mkdir -p build/release
archive="build/release/flowcore-$version.tar.gz"
git archive --format=tar --prefix="flowcore-$version/" HEAD | gzip -n >"$archive.tmp"
mv -- "$archive.tmp" "$archive"
sha256sum "$archive" >"$archive.sha256"
printf '%s\n' "$archive"
