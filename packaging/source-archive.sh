#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
: "${SOURCE_DATE_EPOCH:?set SOURCE_DATE_EPOCH to the release source timestamp}"
case "$SOURCE_DATE_EPOCH" in (*[!0-9]*) exit 2;; esac
version=$(sed -n 's/^project(flowcore VERSION \([^ ]*\) .*/\1/p' "$root/CMakeLists.txt")
test -n "$version"
epoch=$(git -C "$root" log -1 --format=%ct)
if [[ "$SOURCE_DATE_EPOCH" != "$epoch" ]]; then
  echo 'SOURCE_DATE_EPOCH must match the archived Git commit timestamp' >&2
  exit 2
fi
if ! git -C "$root" diff --quiet HEAD --; then
  echo 'Source archive requires a clean tracked checkout' >&2
  exit 2
fi
mkdir -p "$root/build/release"
archive="$root/build/release/flowcore-$version.tar.gz"
git -C "$root" archive --format=tar --prefix="flowcore-$version/" HEAD | gzip -n >"$archive.tmp"
mv -- "$archive.tmp" "$archive"
sha256sum "$archive" >"$archive.sha256"
printf '%s\n' "${archive#"$root"/}"
