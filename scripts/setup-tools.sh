#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"
if [[ -x tools/root/usr/bin/tc ]]; then
    exec tools/root/usr/bin/tc -V
fi
mkdir -p tools/downloads tools/cache tools/logs tools/root
dnf --setopt=cachedir="$project_dir/tools/cache" \
    --setopt=logdir="$project_dir/tools/logs" \
    --setopt=persistdir="$project_dir/tools/cache" \
    download --destdir=tools/downloads iproute-tc
for package in tools/downloads/iproute-tc-*.rpm; do
    rpm -K "$package"
    rpm2cpio "$package" | (cd tools/root && cpio -idm --quiet)
done
tools/root/usr/bin/tc -V
