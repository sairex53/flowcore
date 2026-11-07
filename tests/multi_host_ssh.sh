#!/usr/bin/env bash
# Test the real SSH protocol and a remote listener, on one Linux boot.
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
account=${FLOWCORE_TEST_SSH_USER:-${SUDO_USER:-}}
if [[ $EUID != 0 || $# != 1 || ! $account =~ ^[a-z_][a-z0-9_-]*$ || $account == root ]]; then
    echo "usage: sudo bash tests/multi_host_ssh.sh <new-output-directory> (requires a nonroot login account)" >&2
    exit 2
fi
sshd=$(command -v sshd)
ssh=$(command -v ssh)
out=$1
mkdir -- "$out"
out=$(realpath -- "$out")
keys=$(mktemp -d "$out/ssh-fixture.XXXXXX")
client=fcxs$$-c
server=fcxs$$-s
declare -a created=()
pid=
cleanup() {
    if [[ -n $pid ]]; then
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    fi
    for ns in "${created[@]}"; do
        ip netns delete "$ns"
    done
    rm -rf -- "$keys"
    if [[ -n ${SUDO_UID:-} && -n ${SUDO_GID:-} ]]; then
        chown -R -- "$SUDO_UID:$SUDO_GID" "$out"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
for ns in "$client" "$server"; do
    ip netns add "$ns"
    created+=("$ns")
    ip -n "$ns" link set lo up
done
ip -n "$client" link add data0 type veth peer name data0 netns "$server"
ip -n "$client" address add 198.18.9.1/30 dev data0
ip -n "$server" address add 198.18.9.2/30 dev data0
ip -n "$client" link set data0 up
ip -n "$server" link set data0 up
ssh-keygen -q -t ed25519 -N '' -f "$keys/host"
ssh-keygen -q -t ed25519 -N '' -f "$keys/client"
chmod 711 "$keys"
printf '[198.18.9.2]:23622 %s\n' "$(cut -d ' ' -f 1-2 "$keys/host.pub")" > "$keys/known_hosts"
cat > "$keys/sshd_config" <<EOF
ListenAddress 198.18.9.2:23622
HostKey "$keys/host"
AuthorizedKeysFile "$keys/client.pub"
PidFile "$keys/sshd.pid"
AllowUsers $account
AuthenticationMethods publickey
PasswordAuthentication no
PermitRootLogin no
UsePAM yes
EOF
cat > "$keys/ssh_config" <<EOF
Host test-server
    HostName 198.18.9.2
    Port 23622
    User $account
    IdentityFile "$keys/client"
    IdentitiesOnly yes
    UserKnownHostsFile "$keys/known_hosts"
    StrictHostKeyChecking yes
EOF
mkdir "$keys/bin"
cat > "$keys/bin/ssh" <<'EOF'
#!/bin/sh
exec "$FLOWCORE_TEST_SSH" -F "$FLOWCORE_TEST_SSH_CONFIG" "$@"
EOF
chmod 755 "$keys/bin/ssh"
export FLOWCORE_TEST_SSH=$ssh FLOWCORE_TEST_SSH_CONFIG=$keys/ssh_config
ip netns exec "$server" "$sshd" -D -e -f "$keys/sshd_config" > "$out/sshd.log" 2>&1 &
pid=$!
for ((attempt=0; attempt<30; attempt++)); do
    kill -0 "$pid"
    if ip netns exec "$client" "$ssh" -F "$keys/ssh_config" -oBatchMode=yes -oConnectTimeout=1 test-server true 2>"$out/readiness.log"; then
        break
    fi
    sleep .1
done
python3 - "$PWD" "$out/config.json" <<'PY'
import json
import sys
from pathlib import Path
Path(sys.argv[2]).write_text(json.dumps(dict(owner='ssh-test', direction='forward', capacity_kbit=4000,
    ports=[19000, 19001, 19002], roles={
        'client': dict(interface='data0', address='198.18.9.1'),
        'server': dict(ssh='test-server', checkout=sys.argv[1], interface='data0', address='198.18.9.2')},
    workload=dict(duration=3, api_rate=12, interactive_rate=8, bulk_bytes=131072))))
PY
ip netns exec "$client" env PATH="$keys/bin:$PATH" python3 -B scripts/multi_host_lab.py \
    "$out/config.json" --output "$out/run" --modes unshaped --repetitions 1
