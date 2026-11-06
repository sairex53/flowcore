#!/usr/bin/env bash
set -euo pipefail
compiler=$1
output=$2
source_dir=$3
CCACHE_DISABLE=1 "$compiler" -g -O2 -target bpf -mcpu=v3 -I"$output"   -c "$source_dir/bpf/flow_engine.bpf.c" -o "$output/flow_engine.bpf.o"
