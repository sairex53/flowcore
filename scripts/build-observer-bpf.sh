#!/usr/bin/env bash
set -euo pipefail
bpftool=$1
compiler=$2
output=$3
source_dir=$4
mkdir -p "$output"
"$bpftool" btf dump file /sys/kernel/btf/vmlinux format c >"$output/vmlinux.h"
CCACHE_DISABLE=1 "$compiler" -g -O2 -target bpf -I"$output" \
  -c "$source_dir/bpf/observe.bpf.c" -o "$output/observe.bpf.o"
