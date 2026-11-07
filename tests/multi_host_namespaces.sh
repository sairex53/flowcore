#!/usr/bin/env bash
# Exercise all role processes and the actual shapers on disposable local veths.
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
if [[ $EUID != 0 || $# != 1 ]]; then
    echo "usage: sudo bash tests/multi_host_namespaces.sh <new-output-directory>" >&2
    exit 2
fi
out=$1
mkdir -- "$out"
prefix=fcx$$
client=$prefix-c
server=$prefix-s
router=$prefix-r
bulk=$prefix-b
declare -a created=()
cleanup() {
    for ns in "${created[@]}"; do
        ip netns delete "$ns"
    done
    if [[ -n ${SUDO_UID:-} && -n ${SUDO_GID:-} ]]; then
        chown -R -- "$SUDO_UID:$SUDO_GID" "$out"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
for ns in "$client" "$server" "$router" "$bulk"; do
    ip netns add "$ns"
    created+=("$ns")
    ip -n "$ns" link set lo up
done
for i in 1 2 3; do
    case $i in
        1) ns=$client ;;
        2) ns=$server ;;
        3) ns=$bulk ;;
    esac
    # Both ends are born inside namespaces. The host route table is untouched.
    ip -n "$router" link add "r$i" type veth peer name edge netns "$ns"
    ip -n "$router" address add "198.18.$i.1/30" dev "r$i"
    ip -n "$router" link set "r$i" alias "flowcore-lab:$prefix"
    ip -n "$router" link set "r$i" up
    ip -n "$ns" address add "198.18.$i.2/30" dev edge
    ip -n "$ns" link set edge up
    ip -n "$ns" route add 198.18.0.0/16 via "198.18.$i.1" dev edge
done
ip netns exec "$router" sysctl -q -w net.ipv4.ip_forward=1
cat > "$out/forward.json" <<EOF
{
  "owner": "$prefix", "direction": "forward", "capacity_kbit": 4000,
  "ports": [19000, 19001, 19002],
  "roles": {
    "client": {"namespace": "$client", "interface": "edge", "address": "198.18.1.2"},
    "server": {"namespace": "$server", "interface": "edge", "address": "198.18.2.2"},
    "shaper": {"namespace": "$router", "interface": "r2"},
    "bulk": {"namespace": "$bulk", "interface": "edge", "address": "198.18.3.2"}
  },
  "workload": {"duration": 7, "api_rate": 45, "interactive_rate": 30,
    "deadline_ms": 100, "bulk_bytes": 2097152, "bulk_timeout": 15},
  "impairment": {"interface": "r1", "after_seconds": 2, "delay_ms": 12, "loss_pct": 0.2}
}
EOF
python3 -B scripts/multi_host_lab.py "$out/forward.json" \
    --output "$out/forward" --modes unshaped,fq_codel,cake,static,queue-aware,hybrid \
    --repetitions "${FLOWCORE_TEST_REPETITIONS:-2}" --seed 4711
python3 - "$out/forward.json" "$out/reverse.json" <<'PY'
import json
import sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
data['direction'] = 'reverse'
data['roles']['shaper']['interface'] = 'r1'
del data['roles']['bulk']
data['impairment']['interface'] = 'r2'
Path(sys.argv[2]).write_text(json.dumps(data))
PY
python3 -B scripts/multi_host_lab.py "$out/reverse.json" \
    --output "$out/reverse" --modes static --repetitions 1 --seed 4711
# Exercise the verified transaction RPC separately from strategy qualification.
PYTHONPATH=scripts python3 -B - "$router" "$prefix" "$out" <<'PY'
import json
from pathlib import Path
import sys
from multi_host_lab import Peer

router, owner, output = sys.argv[1:]
peer = Peer('shaper', {'namespace': router, 'interface': 'r2'}, owner, Path(output))
try:
    current = peer.call('configure', mode='hybrid', capacity=4000, critical_pct=40,
                        ports=[19000, 19001, 19002], direction='forward')['allocation']
    desired = dict(current, critical=[1800, 4000], background=[1000, 4000])
    try:
        peer.call('adjust', expected=current,
                  desired=dict(current, critical=[2100, 4000], background=[700, 4000]))
    except RuntimeError as error:
        assert 'outside multi-host lab action budget' in str(error)
    else:
        raise AssertionError('unsafe allocation accepted')
    tx = peer.call('adjust', expected=current, desired=desired)
    assert tx['transaction'] == 'VERIFIED'
    classes = peer.call('sample')['queues']['class']
    rate = {row['handle']: row['rate'] for row in classes if row.get('class') == 'htb'}
    assert rate['1:10'] == 225000 and rate['1:20'] == 125000
    (Path(output) / 'actuation.json').write_text(json.dumps(dict(transaction=tx, classes=classes), indent=2))
finally:
    peer.close()
PY
for dev in r1 r2; do
    root=$(ip -n "$router" -j link show dev "$dev" |
        python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["qdisc"])')
    [[ $root == noqueue ]]
done
