#!/usr/bin/env bash
set -euo pipefail
test "$#" -eq 5
test_root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
out="$5"
mkdir -p -- "$out"
temp="$(mktemp -d "$out/.test-key-XXXXXX")"
trap 'rm -rf -- "$temp"' EXIT
home="$temp/gnupg"
mkdir -m 0700 "$home"
export GNUPGHOME="$home"
gpg --batch --pinentry-mode loopback --passphrase '' \
    --quick-generate-key 'FLOWCORE ephemeral CI signing test key <flowcore-test@example.invalid>' rsa3072 sign 1d
fingerprint="$(gpg --batch --with-colons --list-secret-keys |
    awk -F: '$1 == "fpr" { print $10; exit }')"
test -n "$fingerprint"
export FLOWCORE_SIGNING_FINGERPRINT="$fingerprint"
for source in "$1" "$2" "$3" "$4"; do
    cp -- "$source" "$temp/$(basename -- "$source")"
done
rpm_path="$(find "$temp" -maxdepth 1 -name 'flowcore-*.rpm' ! -name '*.src.rpm' -print -quit)"
srpm_path="$(find "$temp" -maxdepth 1 -name 'flowcore-*.src.rpm' -print -quit)"
source_path="$(find "$temp" -maxdepth 1 -name 'flowcore-*.tar.gz' -print -quit)"
sbom_path="$(find "$temp" -maxdepth 1 -name '*spdx*.json' -print -quit)"
test -n "$rpm_path" && test -n "$srpm_path" && test -n "$source_path" && test -n "$sbom_path"
"$test_root/packaging/sign-artifacts.sh" --rpm-only "$rpm_path" "$srpm_path"
provenance="$temp/release-provenance.json"
python3 -B "$test_root/packaging/release-provenance.py" --source-archive "$source_path" \
    --artifacts "$rpm_path" "$srpm_path" "$sbom_path" --output "$provenance" --signing-status test-key
"$test_root/packaging/sign-artifacts.sh" --detached-only \
    "$rpm_path" "$srpm_path" "$source_path" "$sbom_path" "$provenance"

rpmroot="$temp/rpmroot"
mkdir -p "$rpmroot"
rpm --root "$rpmroot" --initdb
gpg --batch --armor --export "$fingerprint" > "$temp/test-public.asc"
rpm --root "$rpmroot" --import "$temp/test-public.asc"
rpm --root "$rpmroot" --checksig --verbose "$rpm_path"
rpm --root "$rpmroot" --checksig --verbose "$srpm_path"
for artifact in "$rpm_path" "$srpm_path" "$source_path" "$sbom_path" "$provenance"; do
    gpg --batch --verify "$artifact.asc" "$artifact" >/dev/null
done
python3 - "$out/signing-test-report.json" <<'PY'
import json
import sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({
    "schema": 1,
    "official_signing": "BLOCKED_NO_PRODUCTION_KEY",
    "ephemeral_test_key": "PASS",
    "rpm_internal_signatures": "verified against disposable RPM key database",
    "detached_signatures": "verified for source, RPM, SRPM, SPDX SBOM, and provenance",
    "private_key_persisted": False
}, indent=2, sort_keys=True) + "\n")
PY
printf '%s\n' 'PASS: ephemeral test-key signing and verification; official signing remains blocked'
