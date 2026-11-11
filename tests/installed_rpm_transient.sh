#!/usr/bin/env bash
# Isolated, labelled RPM payload and real systemd transient units. No host RPM install.
set -euo pipefail
test "$#" = 1 && test "$EUID" = 0 && test "$(getenforce)" = Enforcing
root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
rpm="$(realpath -- "$1")"
operator="${SUDO_UID:?invoke using sudo from the workspace operator}"
operator_gid="${SUDO_GID:?invoke using sudo from the workspace operator}"
test "$operator" -gt 0
rpm -K --nosignature "$rpm"
stage="$(mktemp -d "$root/build/fc-root-XXXX")"
tag="flowcore-rpm-fixture-$$"
monitor="$tag-monitor"
sdk="$tag-sdk"
helper="$tag-helper"
sdk_socket="/run/$sdk.sock"
helper_socket="/run/$helper.sock"
units=("$monitor.service" "$sdk.service" "$sdk.socket" "$helper.service" "$helper.socket")

cleanup() {
    for unit in "${units[@]}"; do
        systemctl stop "$unit" 2>/dev/null || :
        journalctl -b -u "$unit" --no-pager || :
    done
    rm -f -- "$sdk_socket" "$helper_socket"
    rm -rf -- "$stage"
}
trap cleanup EXIT
host_net() {
    python3 - <<'PYHOST'
import json
import shutil
import subprocess

def query(*argv):
    return json.loads(subprocess.run(argv, check=True, capture_output=True, text=True).stdout)

links = query('ip', '-j', '-details', 'link', 'show')
link_fields = ('ifindex', 'ifname', 'flags', 'mtu', 'qdisc', 'operstate',
               'linkmode', 'group', 'txqlen', 'link_type', 'address',
               'broadcast', 'link', 'link_index', 'master', 'linkinfo',
               'num_tx_queues', 'num_rx_queues')
links = [{key: row[key] for key in link_fields if key in row} for row in links]
addresses = query('ip', '-j', 'address', 'show')
addresses = [{'ifname': row.get('ifname'),
              'addr_info': sorted(({key: item[key] for key in
                  ('family', 'local', 'prefixlen', 'scope', 'label', 'flags', 'peer', 'broadcast')
                  if key in item} for item in row.get('addr_info', [])),
                  key=lambda item: json.dumps(item, sort_keys=True))}
             for row in addresses]
route_fields = ('dst', 'dev', 'gateway', 'protocol', 'table', 'scope',
                'prefsrc', 'metric', 'type', 'flags', 'tos', 'priority',
                'nhid', 'via', 'encap')
routes = []
for args in (('ip', '-j', 'route', 'show', 'table', 'all'),
             ('ip', '-6', '-j', 'route', 'show', 'table', 'all')):
    rows = query(*args)
    routes.extend({key: row[key] for key in route_fields if key in row} for row in rows)
routes.sort(key=lambda item: json.dumps(item, sort_keys=True))

def stable(value):
    dynamic = {'stats', 'stats2', 'bytes', 'packets', 'drops', 'overlimits',
               'requeues', 'backlog', 'qlen', 'used', 'lastuse'}
    if isinstance(value, dict):
        return {key: stable(item) for key, item in sorted(value.items()) if key not in dynamic}
    if isinstance(value, list):
        return [stable(item) for item in value]
    return value

traffic_control = []
tc = shutil.which('tc')
if tc:
    for kind in ('qdisc', 'class', 'filter'):
        rows = query(tc, '-j', '-details', kind, 'show')
        traffic_control.append((kind, stable(rows)))
print(json.dumps({'links': links, 'addresses': addresses,
                  'routes': routes, 'traffic_control': traffic_control},
                 sort_keys=True, separators=(',', ':')))
PYHOST
}
before="$(host_net)"
test ! -e "$sdk_socket" && test ! -e "$helper_socket"

chmod 0755 "$stage"
(cd "$stage" && rpm2cpio "$rpm" | cpio -idmu --quiet)
for dir in bin lib lib64; do ln -s "usr/$dir" "$stage/$dir"; done
ln -s bash "$stage/usr/bin/sh"
python_version="$(readlink /usr/bin/python3)"
ln -s "$python_version" "$stage/usr/bin/python3"
loader="$(readelf -l /usr/bin/bash | sed -n 's/.*interpreter: \(.*\)]/\1/p')"
test -n "$loader"
python_lib="/usr/lib/$python_version"
mkdir -p "$stage"/{run,dev,proc,sys,tmp,work,usr/lib64} "$stage$python_lib"
chmod 1777 "$stage/tmp"
for name in bash dirname "$python_version"; do touch "$stage/usr/bin/$name"; done
touch "$stage$loader"
install -d -m 0700 -o daemon -g daemon "$stage/work/monitor" "$stage/work/monitor-state"
install -d -m 0700 -o bin -g bin "$stage/work/control" "$stage/work/control-state"
install -d -m 0700 -o "$operator" -g "$operator_gid" "$stage/work/output"

python3 - "$stage" "$operator" "$(id -u daemon)" <<'PY'
import json
import sys
from pathlib import Path
stage, operator, reader = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
etc = stage/'etc/flowcore'
config = etc/'service.json'
data = json.loads(config.read_text())
data.setdefault('intent_api', {'priorities':['standard']})['applications'] = {'fixture-app':operator}
config.write_text(json.dumps(data))
(etc/'helper.json').write_text(json.dumps(dict(version=1,operators=[operator],readers=[reader])))
PY
getent passwd root daemon bin nobody > "$stage/etc/passwd"
getent group root daemon bin nobody > "$stage/etc/group"
setfiles -r "$stage" /etc/selinux/targeted/contexts/files/file_contexts "$stage"
ls -Zd "$stage/usr/bin/flowcore" "$stage/etc/flowcore" "$stage/work/output"
common=(-p "RootDirectory=$stage" -p MountAPIVFS=yes -p NoNewPrivileges=yes
        -p ProtectSystem=strict -p ReadWritePaths=+/work -p PrivateTmp=yes -p PrivateDevices=yes
        -p ProtectHome=yes -p RestrictNamespaces=yes -p 'RestrictAddressFamilies=AF_UNIX AF_NETLINK'
        -p "BindReadOnlyPaths=/usr/bin/bash /usr/bin/dirname /usr/bin/$python_version /usr/lib64 $python_lib $loader")
zero=(-p CapabilityBoundingSet= -p Type=notify -p WatchdogSec=15 -p TimeoutStartSec=10)
listener=(--socket-property=PassCredentials=yes "--socket-property=SocketGroup=$operator_gid"
          --socket-property=SocketMode=0660 --socket-property=RemoveOnStop=yes)
systemd-run --unit="$monitor" --collect "${common[@]}" "${zero[@]}" --uid=daemon --gid=daemon \
    --setenv=FLOWCORE_RUNTIME=/work/monitor --setenv=FLOWCORE_STATE=/work/monitor-state \
    /usr/bin/flowcored --monitor-only --config /etc/flowcore/service.json
FLOWCORE_RUNTIME="$stage/work/monitor" "$stage/usr/bin/flowcore" status
systemd-run --unit="$sdk" --collect "${common[@]}" "${zero[@]}" --uid=bin --gid=bin \
    -p Restart=on-failure -p RestartSec=1 \
    --setenv=FLOWCORE_RUNTIME=/work/control --setenv=FLOWCORE_STATE=/work/control-state \
    "${listener[@]}" "--socket-property=ListenSequentialPacket=$sdk_socket" --socket-property=FileDescriptorName=intent \
    /usr/bin/flowcored --control-only --config /etc/flowcore/service.json
systemd-run --unit="$helper" --collect "${common[@]}" --uid=nobody --gid=nobody \
    -p PrivateNetwork=yes -p CapabilityBoundingSet=CAP_NET_ADMIN -p AmbientCapabilities=CAP_NET_ADMIN \
    -p ProtectProc=invisible -p ProcSubset=pid -p OpenFile=/proc/1/ns/net:host-netns:read-only \
    -p 'SystemCallFilter=~@mount @module @raw-io @reboot @swap' -p SystemCallErrorNumber=EPERM \
    "${listener[@]}" "--socket-property=ListenSequentialPacket=$helper_socket" --socket-property=FileDescriptorName=control \
    /usr/bin/python3 -E -s -B /usr/share/flowcore/scripts/network_helper.py
ls -lZ "$sdk_socket" "$helper_socket"

python3 - "$stage" "$monitor" "$sdk" "$helper" "$sdk_socket" "$helper_socket" "$operator" "$operator_gid" <<'PY'
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time

stage = Path(sys.argv[1])
monitor, sdk, helper, sdk_socket, helper_socket = sys.argv[2:7]
operator, gid = map(int, sys.argv[7:])
cli = stage/'usr/bin/flowcore'
def run(argv, ok=True, **kwargs):
    process = subprocess.run(argv, text=True, capture_output=True, timeout=12, **kwargs)
    if (process.returncode == 0) != ok:
        raise AssertionError((argv[0], process.returncode, process.stderr[:400]))
    return process.stdout
def drop(uid):
    return ['setpriv', f'--reuid={uid}', f'--regid={gid}', '--clear-groups',
            '--no-new-privs', '--bounding-set=-all']
def pid(unit):
    return int(run(['systemctl','show','-p','MainPID','--value',unit+'.service']))
def wait(call):
    for _ in range(60):
        try:
            value = call()
            if value:
                return value
        except (OSError, AssertionError, ValueError):
            pass
        time.sleep(.1)
    raise AssertionError('fixture wait timed out')
def process(unit, mask):
    p = pid(unit)
    fields = dict(line.split(':',1) for line in Path(f'/proc/{p}/status').read_text().splitlines() if ':' in line)
    assert int(fields['Uid'].split()[1]) > 0 and int(fields['NoNewPrivs']) == 1
    assert all(int(fields[k],16)==mask for k in ('CapEff','CapPrm','CapBnd','CapInh','CapAmb'))
    print(unit, p, Path(f'/proc/{p}/attr/current').read_text().strip('\0\n'), mask)
    return p
code = '''
import json,socket,sys
with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as peer:
    peer.settimeout(4)
    peer.connect(sys.argv[1])
    peer.sendall(sys.argv[2].encode())
    print(json.dumps(json.loads(peer.recv(65536))['ok']))
'''
def register(name, uid=operator, ok=True):
    request = dict(op='intent_v2',version=2,action='register',isolation='shared',
                   intent=dict(name=name,priority='standard'))
    return run([*drop(uid),'/usr/bin/python3','-I','-B','-c',code,sdk_socket,json.dumps(request)],ok=ok)
def network(*args, ok=True):
    output=run([*drop(operator),cli,'network','--socket',helper_socket,*args],ok=ok)
    return json.loads(output) if ok else None
def control(*args):
    return json.loads(run([cli,*args],env=dict(os.environ,FLOWCORE_RUNTIME=str(stage/'work/control'))))
def stamp(state):
    return ['--epoch',state['epoch'],'--revision',str(state['revision'])]

with ThreadPoolExecutor(max_workers=2) as executor:
    assert all(json.loads(item) for item in executor.map(register,['fixture-app','fixture-app']))
assert json.loads(register('forged-service')) is False
register('fixture-app', uid=2, ok=False)
process(monitor,0)
original = process(sdk,0)
initial = network('status')
helper_pid = process(helper,4096)
assert os.readlink(f'/proc/{helper_pid}/ns/net') != os.readlink('/proc/1/ns/net')
def helper_wire(uid, operation, **fields):
    message = json.dumps(dict(op=operation, version=1, **fields))
    return json.loads(run([*drop(uid),'/usr/bin/python3','-I','-B','-c',code,helper_socket,message]))

assert helper_wire(2, 'status')
assert not helper_wire(2, 'lab.create', epoch=initial['epoch'], revision=initial['revision'])
assert not helper_wire(operator, 'lab.create', epoch=initial['epoch'], revision=initial['revision'], namespace=1)
output=stage/'work/output'
alias=stage/'work/alias'
alias.symlink_to(output)
network('publish','--directory',str(alias),'--name','bad.json',ok=False)
network('publish','--directory',str(output),'--name','../bad.json',ok=False)
result=network('exercise','--directory',str(output),'--name','verified.json')
assert [row['limit_packets'] for row in result['observations']] == [None,None,128,None,None]
assert not result['observations'][-1]['lab_up']
file=output/'verified.json'
assert file.stat().st_uid==operator and file.stat().st_mode & 0o777==0o600
content=file.read_bytes()
network('publish','--directory',str(output),'--name','verified.json',ok=False)
assert file.read_bytes()==content
network('create',*stamp(initial),ok=False)
latest=network('status')
with ThreadPoolExecutor(max_workers=2) as executor:
    race = [executor.submit(helper_wire, operator, 'lab.create', epoch=latest['epoch'],
                            revision=latest['revision']) for _ in range(2)]
    assert sum(item.result() for item in race)==1
run(['systemctl','kill','--kill-whom=main','--signal=SIGKILL',helper+'.service'])
wait(lambda: not Path(f'/proc/{helper_pid}/ns/net').exists())
recovered=wait(lambda: network('status'))
assert recovered['epoch']!=latest['epoch'] and not recovered['lab_up']
config=stage/'etc/flowcore/service.json'
settings=json.loads(config.read_text())
settings['log_level']='error'
config.write_text(json.dumps(settings))
run(['systemctl','kill','--kill-whom=main','--signal=SIGHUP',sdk+'.service'])
wait(lambda: control('ops','status')['checkpoint']['generation']>=1)
run(['systemctl','kill','--kill-whom=main','--signal=SIGKILL',sdk+'.service'])
wait(lambda: pid(sdk) not in (0,original))
wait(lambda: control('status')['previous_unclean'])
assert json.loads(register('fixture-app'))
print('PASS RPM payload with Enforcing systemd, distinct UIDs, publication, reload and crash recovery')
PY
test "$(host_net)" = "$before"
test "$(getenforce)" = Enforcing
