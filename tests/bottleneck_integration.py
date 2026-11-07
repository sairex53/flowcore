"""Privileged end-to-end checks for lab admission, IFB and path diagnostics."""
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from benchmark import BINDIR, ENV
from kernel_api_experiment import host_identity
from directional_qos import Ingress
from service_lab import ServiceLab


@contextmanager
def daemon(directory, monitor_only=False):
    env = dict(ENV, FLOWCORE_RUNTIME=str(directory/'runtime'), FLOWCORE_STATE=str(directory/'state'))
    args = [BINDIR/'flowcored', '--monitor-only'] if monitor_only else [BINDIR/'flowcored']
    with (directory/'daemon.log').open('w') as log:
        proc = subprocess.Popen(args, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic()+5
            while not (directory/'runtime/control.sock').exists():
                assert proc.poll() is None and time.monotonic() < deadline
                time.sleep(.05)
            yield env
        finally:
            proc.terminate()
            proc.wait(timeout=10)
        assert proc.returncode == 0


def call(env, *args, expected=0):
    result = subprocess.run([BINDIR/'flowcore', *args], capture_output=True, text=True,
                            env=env, timeout=10)
    assert result.returncode == expected, result.stderr
    return result.stdout if expected == 0 else result.stderr


def ipv6_counters(env, snapshot):
    original = os.stat('/proc/self/ns/net')
    with ExitStack() as stack:
        original_ns = stack.enter_context(open('/proc/self/ns/net', 'rb'))
        sockets = []
        for holder in snapshot['namespaces']:
            target = stack.enter_context(open(f'/proc/{holder}/ns/net', 'rb'))
            private = os.fstat(target.fileno())
            assert (private.st_dev, private.st_ino) != (original.st_dev, original.st_ino)
            os.setns(target.fileno(), os.CLONE_NEWNET)
            try:
                sock = stack.enter_context(socket.socket(socket.AF_INET6))
                sock.settimeout(2)
                sockets.append(sock)
            finally:
                os.setns(original_ns.fileno(), os.CLONE_NEWNET)
        client, server = sockets
        server.bind(('fd42:203::2', 9000))
        server.listen(1)
        client.connect(server.getsockname())
        peer = stack.enter_context(server.accept()[0])
        peer.settimeout(2)
        client.sendall(b'v6')
        assert peer.recv(2, socket.MSG_WAITALL) == b'v6'
        peer.sendall(b'ok')
        assert client.recv(2, socket.MSG_WAITALL) == b'ok'
        before = snapshot['tc']
        after = json.loads(call(env, 'lab', 'inspect'))['tc']
        for prior, current in ((before['traffic_classes'], after['traffic_classes']),
                               (before['ingress']['classes'], after['ingress']['classes'])):
            assert current['critical']['stats']['bytes'] > prior['critical']['stats']['bytes']


def route_event(env, holder):
    original = os.stat('/proc/self/ns/net')
    with open(f'/proc/{holder}/ns/net', 'rb') as target:
        private = os.fstat(target.fileno())
        assert (private.st_dev, private.st_ino) != (original.st_dev, original.st_ino)
        subprocess.run(['nsenter', f'--net=/proc/self/fd/{target.fileno()}', '--',
                        'ip', 'route', 'add', '198.18.0.0/24', 'dev', 'fc0'],
                       pass_fds=(target.fileno(),), check=True, timeout=5, env=env)
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        info = json.loads(call(env, 'path', 'diagnostics', '--json'))
        if (any(row.get('destination') == '198.18.0.0/24' for row in info['routes']) and
                any(row['type'] == 24 for row in info['events'])):
            return
        time.sleep(.1)
    raise AssertionError('owned route change was not observed')


def foreign_hook(directory):
    directory.mkdir()
    lab = ServiceLab(directory)
    try:
        lab.start()
        lab.tc('qdisc', 'add', 'dev', 'fc0', 'handle', 'ffff:', 'ingress')
        before = Ingress.read(lab, 'qdisc', 'show', 'dev', 'fc0')
        try:
            Ingress(2000).install(lab)
        except ValueError as error:
            assert 'existing ingress hook' in str(error)
        else:
            raise AssertionError('foreign ingress hook accepted')
        after = Ingress.read(lab, 'qdisc', 'show', 'dev', 'fc0')
        assert [(r['kind'], r['handle']) for r in before] == [(r['kind'], r['handle']) for r in after]
        assert {r['name'] for r in lab.network(0)['interfaces']} == {'lo', 'fc0'}
    finally:
        lab.close()


def main():
    assert os.geteuid() == 0, 'run as root in a disposable test environment'
    original = host_identity()
    with tempfile.TemporaryDirectory(dir=ROOT/'build', prefix='bn-') as path:
        work = Path(path)
        foreign_hook(work/'foreign')
        with daemon(work, monitor_only=True) as env:
            assert json.loads(call(env, 'bottleneck', 'inspect'))['scope'] == 'host_read_only'
            assert 'forbidden' in call(env, 'lab', 'up', '--ingress-kbit', '2000', expected=1)
        with daemon(work) as env:
            assert 'expected integer' in call(env, 'lab', 'up', '--ingress-kbit', '99', expected=1)
            call(env, 'lab', 'up')
            old = json.loads(call(env, 'lab', 'inspect'))
            assert 'starting a new lab' in call(env, 'lab', 'up', '--ingress-kbit', '2000', expected=1)
            assert json.loads(call(env, 'lab', 'inspect'))['namespaces'] == old['namespaces']
            call(env, 'lab', 'down')
            call(env, 'lab', 'up', '--ingress-kbit', '2000')
            current = json.loads(call(env, 'lab', 'inspect'))
            tc = current['tc']
            assert tc['ingress']['verified'] and tc['ingress']['capacity_kbit'] == 2000
            assert next(row for row in tc['class'] if row['handle'] == '1:1')['rate'] == 2_500_000
            qos = json.loads(call(env, 'qos', 'direction'))
            assert qos['ingress_policy']['capacity_kbit'] == 2000 and qos['egress_policy'] == 'default'
            assert json.loads(call(env, 'capacity', 'estimate'))['scope'] == 'owned_lab_sender'
            watch = call(env, 'bottleneck', 'watch', '--watch', '.1', '--count', '2')
            assert [json.loads(line)['schema'] for line in watch.splitlines()] == [1, 1]
            ipv6_counters(env, current)
            route_event(env, current['namespaces'][0])
            call(env, 'lab', 'down')
            for pid in old['namespaces'] + current['namespaces']:
                assert not Path(f'/proc/{pid}/ns/net').exists()
    assert host_identity() == original
    print('PASS: monitor ACL, foreign ingress preserved, explicit IFB admission, independent rates, IPv6 counters, watch, routes and cleanup')


if __name__ == '__main__':
    main()
