#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
: "${SOURCE_DATE_EPOCH:?set SOURCE_DATE_EPOCH to the release source timestamp}"
case "$SOURCE_DATE_EPOCH" in (*[!0-9]*) exit 2;; esac
version=$(sed -n 's/^project(flowcore VERSION \([^ ]*\) .*/\1/p' CMakeLists.txt)
test -n "$version"
mkdir -p build/release
archive="build/release/flowcore-$version.tar.gz"
shopt -s nullglob
tar --sort=name --format=gnu --mtime="@$SOURCE_DATE_EPOCH" --owner=0 --group=0 --numeric-owner \
  --exclude='__pycache__' --transform="s|^|flowcore-$version/|" -cf - \
  CMakeLists.txt .github bpf config examples include monitoring packaging scenarios scripts src tests *.md LICENSE* |
  gzip -n >"$archive.tmp"
mv -- "$archive.tmp" "$archive"
sha256sum "$archive" >"$archive.sha256"
printf '%s\n' "$archive"
