"""Trusted root runner. The actuator and test clients themselves run as non-root."""
import concurrent.futures
import fcntl
import inspect
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = '/usr/bin/python3'
OPERATOR = int(os.environ.get('SUDO_UID', '0')) or 65532
READER = 65533
OTHER = 65531
HELPER = 65534


def drop(uid):
    return ['/usr/bin/setpriv', f'--reuid={uid}', f'--regid={uid}', '--clear-groups',
            '--bounding-set=-all', '--inh-caps=-all', '--ambient-caps=-all', '--no-new-privs']


def request(path, raw):
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
        client.settimeout(4)
        client.connect(str(path))
        try:
            client.sendall(raw)
        except BrokenPipeError:
            pass
        return json.loads(client.recv(4096))


def call(path, peer_uid, op=None, raw=None, **fields):
    if raw is None:
        raw = json.dumps(dict(version=1, op=op, **fields)).encode()
    if peer_uid == 0:
        return request(path, raw)
    code = ('import json,socket,sys\n' + inspect.getsource(request)
            + '\nprint(json.dumps(request("control.sock",sys.stdin.buffer.read())))')
    proc = subprocess.run([*drop(peer_uid), PYTHON, '-B', '-c', code],
                          cwd=path.parent, input=raw, capture_output=True, timeout=6)
    assert proc.returncode == 0, proc.stderr.decode()
    return json.loads(proc.stdout)


def install_seccomp(names):
    import ctypes
    import errno
    library = ctypes.CDLL('libseccomp.so.2')
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
    assert context
    try:
        for name in names:
            syscall = library.seccomp_syscall_resolve_name(name.encode())
            if syscall >= 0:
                assert library.seccomp_rule_add(context, 0x00050000 | errno.EPERM, syscall, 0) == 0
        assert library.seccomp_load(context) == 0
    finally:
        library.seccomp_release(context)


def worker(fds, code_dir, blocked):
    install_seccomp(blocked)
    copies = [fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10) for fd in [*fds, code_dir]]
    for fd in [*fds, code_dir]:
        os.close(fd)
    for index, fd in enumerate(copies[:-1], 3):
        os.dup2(fd, index, inheritable=False)
        os.close(fd)
    os.environ.update(LISTEN_PID=str(os.getpid()), LISTEN_FDS='3',
                      LISTEN_FDNAMES='control:host-netns:test-config')
    sys.path.insert(0, f'/proc/self/fd/{copies[-1]}')
    from network_helper import main
    sys.path.pop(0)
    os.close(copies[-1])
    sys.exit(main())


def launch(listener, witness, cfg, log, blocked, isolated=True, join=None):
    code_dir = os.open(ROOT/'scripts', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fds = [listener.fileno(), witness.fileno(), cfg.fileno()]
        cfg.seek(0)
        caps = drop(HELPER)
        caps = [item.replace('=-all', '=-all,+net_admin') for item in caps]
        code = ('import fcntl,json,os,sys\n' + inspect.getsource(install_seccomp) + inspect.getsource(worker)
                + '\nworker(list(map(int,sys.argv[1:4])),int(sys.argv[4]),json.loads(sys.argv[5]))')
        argv = [*caps, PYTHON, '-E', '-s', '-B', '-c', code,
                *map(str, [*fds, code_dir]), json.dumps(blocked)]
        if join is not None:
            argv = ['/usr/bin/nsenter', f'--net=/proc/{join}/ns/net', '--', *argv]
        elif isolated:
            argv = ['/usr/bin/unshare', '--net', '--', *argv]
        return subprocess.Popen(argv, pass_fds=(*fds, code_dir), stdout=log, stderr=log)
    finally:
        os.close(code_dir)


def wait_ready(proc, path):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError('helper exited during startup')
        status = Path(f'/proc/{proc.pid}/status').read_text()
        if 'CapEff:\t0000000000001000' in status:
            assert 'CapBnd:\t0000000000001000' in status
            assert 'NoNewPrivs:\t1' in status
            if 'Seccomp:\t2' not in status:
                continue
            if f'Uid:\t{HELPER}\t{HELPER}\t{HELPER}\t{HELPER}' in status:
                return call(path, 0, 'status')['result']
        time.sleep(.01)
    raise AssertionError('helper did not acquire the expected minimal privileges')


def main():
    if os.geteuid() != 0:
        raise SystemExit('run with sudo in a trusted checkout')
    groups = subprocess.check_output(['/usr/bin/systemd-analyze', 'syscall-filter',
                                      '@mount', '@module', '@raw-io', '@reboot', '@swap'], text=True)
    blocked = [line.strip() for line in groups.splitlines()
               if line.startswith(' ') and line.strip().isidentifier()]
    assert 'mount' in blocked and 'reboot' in blocked
    blocked += ['unshare', 'setns']
    host = subprocess.check_output(['/usr/bin/ip', '-j', 'link', 'show'])
    with tempfile.TemporaryDirectory(dir=ROOT/'build', prefix='helper-test-') as directory:
        work = Path(directory)
        work.chmod(0o755)
        path = work/'control.sock'
        cfgpath = work/'config.json'
        cfgpath.write_text(json.dumps(dict(version=1, operators=[OPERATOR], readers=[READER])))
        cfgpath.chmod(0o644)
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener, \
             open('/proc/self/ns/net', 'rb') as witness, cfgpath.open('rb') as cfg, \
             (work/'audit.log').open('w+') as log:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            listener.bind(str(path))
            path.chmod(0o666)  # Exercise protocol denial even after connecting.
            listener.listen(8)
            def refused(isolated):
                wrong = launch(listener, witness, cfg, log, blocked, isolated)
                code = wrong.wait(timeout=4)
                log.seek(0)
                text = log.read()
                log.seek(0, os.SEEK_END)
                assert code == 1 and 'startup_failed' in text.splitlines()[-1], (code, text)
            refused(False)
            cfgpath.chmod(0o666)
            refused(True)
            cfgpath.chmod(0o644)
            epochs = set()
            for cycle in range(4):
                proc = launch(listener, witness, cfg, log, blocked)
                try:
                    state = wait_ready(proc, path)
                    epoch = state['epoch']
                    assert epoch not in epochs and state['revision'] == 0 and not state['lab_up']
                    if epochs:
                        stale = call(path, OPERATOR, 'lab.create', epoch=next(iter(epochs)), revision=0)
                        assert stale['code'] == 'stale_revision'
                    epochs.add(epoch)
                    assert call(path, OTHER, 'status')['code'] == 'forbidden'
                    assert call(path, READER, 'status')['ok']
                    if cycle == 0:
                        duplicate = launch(listener, witness, cfg, log, blocked, join=proc.pid)
                        assert duplicate.wait(timeout=4) == 1
                        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as disconnected:
                            disconnected.connect(str(path))
                        assert call(path, 0, 'status')['result'] == state
                        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as delegated:
                            delegated.settimeout(4)
                            delegated.connect(str(path))
                            code = ('import socket,sys; peer=socket.socket(fileno=int(sys.argv[1])); '
                                    'peer.sendall(sys.argv[2].encode())')
                            subprocess.run([*drop(OPERATOR), PYTHON, '-B', '-c', code,
                                            str(delegated.fileno()), json.dumps(dict(version=1, op='status'))],
                                           pass_fds=(delegated.fileno(),),
                                           check=True, timeout=4)
                            assert json.loads(delegated.recv(4096))['code'] == 'forbidden'
                        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as ancillary, \
                             open('/dev/null', 'rb') as null:
                            ancillary.settimeout(4)
                            ancillary.connect(str(path))
                            ancillary.sendmsg([b'{"version":1,"op":"status"}'],
                                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack('=i', null.fileno()))])
                            assert json.loads(ancillary.recv(4096))['code'] in ('forbidden', 'invalid_size')
                        if os.environ.get('SUDO_UID') and OPERATOR != 65532:
                            cli = subprocess.check_output([*drop(OPERATOR), ROOT/'build/flowcore',
                                'network', '--socket', path, 'status'], timeout=5)
                            assert json.loads(cli) == state
                    assert call(path, READER, 'lab.create', epoch=epoch, revision=0)['code'] == 'forbidden'
                    assert call(path, OPERATOR, 'lab.create', epoch=epoch, revision=0,
                                service='forged', uid=0)['code'] == 'invalid_request'
                    assert call(path, OPERATOR, raw=b'x'*4096)['code'] == 'invalid_size'
                    assert call(path, OPERATOR, raw=b'{')['code'] == 'invalid_request'
                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                        attempts = list(pool.map(lambda _: call(path, OPERATOR, 'lab.create',
                                                                epoch=epoch, revision=0), range(2)))
                    assert sum(item['ok'] for item in attempts) == 1, attempts
                    assert any(item.get('code') == 'stale_revision' for item in attempts)
                    time.sleep(.06)
                    reply = call(path, OPERATOR, 'queue.set', epoch=epoch, revision=1, limit_packets=64)
                    assert reply.get('result', {}).get('limit_packets') == 64, reply
                    time.sleep(.06)
                    if cycle == 0:
                        assert call(path, OPERATOR, 'queue.set', epoch=epoch, revision=2,
                                    limit_packets=128)['result']['limit_packets'] == 128
                        time.sleep(.06)
                        assert call(path, OPERATOR, 'queue.clear', epoch=epoch,
                                    revision=3)['result']['limit_packets'] is None
                        time.sleep(.06)
                        assert not call(path, OPERATOR, 'lab.remove', epoch=epoch,
                                        revision=4)['result']['lab_up']
                        proc.terminate()
                        assert proc.wait(timeout=4) == 0
                    elif cycle in (1, 2):
                        proc.kill()
                        assert proc.wait(timeout=4) == -9
                    else:
                        with open(f'/proc/{proc.pid}/ns/net', 'rb') as pinned:
                            target = ['/usr/bin/nsenter', f'--net=/proc/self/fd/{pinned.fileno()}',
                                      '--', '/usr/bin/ip']
                            subprocess.run([*target, 'link', 'add', 'foreign0', 'type', 'dummy'],
                                           pass_fds=(pinned.fileno(),), check=True, timeout=4)
                            assert call(path, OPERATOR, 'lab.remove', epoch=epoch,
                                        revision=2)['code'] == 'network_failed'
                            assert proc.wait(timeout=4) == 1
                            rows = json.loads(subprocess.check_output([*target, '-j', 'link', 'show'],
                                                pass_fds=(pinned.fileno(),), timeout=4))
                            assert {row['ifname'] for row in rows} == {'lo', 'fch0', 'fch1', 'foreign0'}
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=4)
                    log.seek(0)
                    evidence = log.read()
                    log.seek(0, os.SEEK_END)
                    if sys.exc_info()[0] is not None:
                        print(evidence, file=sys.stderr)
            assert 'network_committed' in evidence and 'network_failed' in evidence
    assert host == subprocess.check_output(['/usr/bin/ip', '-j', 'link', 'show'])
    print('PASS: non-root helper, CAP_NET_ADMIN only, seccomp, peer ACL, protocol bounds, concurrent revision, '
          'native veth/qdisc, SIGKILL recovery, foreign object refusal and unchanged host links')


if __name__ == '__main__':
    main()
