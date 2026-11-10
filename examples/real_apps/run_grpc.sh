#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 -B grpc_dependencies.py
generated_dir=${FLOWCORE_GRPC_PROTO_DIR:-${TMPDIR:-/tmp}/flowcore-grpc-${UID:-0}}
mkdir -p "$generated_dir"
python3 -m grpc_tools.protoc -I . --python_out="$generated_dir" --grpc_python_out="$generated_dir" grpc_app.proto
export FLOWCORE_GRPC_PROTO_DIR="$generated_dir"
exec python3 grpc_server.py "$@"
