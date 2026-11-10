"""Small installed runtime trust boundaries for configuration and SDK activation."""
import os
from pathlib import Path
import socket
import stat


def root_file(path, limit):
    parts = os.fspath(path).split('/')
    if len(parts) < 2 or parts[0] or any(part in ('', '.', '..') for part in parts[1:]):
        raise ValueError('expected a canonical absolute configuration path')
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in parts[1:-1]:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=directory)
            os.close(directory)
            directory = fd
            info = os.fstat(fd)
            if info.st_uid or info.st_mode & 0o022:
                raise PermissionError('untrusted configuration directory')
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid or info.st_mode & 0o022 or info.st_nlink != 1:
                raise PermissionError('untrusted configuration file')
            value = stream.read(limit + 1)
            if len(value) > limit:
                raise ValueError('configuration exceeds size limit')
            return value
    finally:
        os.close(directory)


def activated_intent_socket():
    names = os.environ.pop('LISTEN_FDNAMES', '')
    count = os.environ.pop('LISTEN_FDS', '')
    pid = os.environ.pop('LISTEN_PID', '')
    if (names, count, pid) != ('intent', '1', str(os.getpid())):
        raise ValueError('expected one named systemd SDK socket')
    sock = socket.socket(fileno=3)
    try:
        os.set_inheritable(3, False)
        if (sock.family != socket.AF_UNIX or sock.type != socket.SOCK_SEQPACKET
                or not sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
                or not sock.getsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED)):
            raise ValueError('invalid systemd SDK socket')
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


def monotonic_offset(pid):
    with Path(f'/proc/{pid}/timens_offsets').open('rb') as stream:
        data = stream.read(513)
    if len(data) > 512:
        raise ValueError('invalid time namespace offsets')
    fields = [row.split() for row in data.splitlines() if row.startswith(b'monotonic ')]
    if len(fields) != 1 or len(fields[0]) != 3:
        raise ValueError('monotonic time namespace offset unavailable')
    seconds, nanoseconds = map(int, fields[0][1:])
    if not 0 <= nanoseconds < 10**9:
        raise ValueError('invalid monotonic time namespace offset')
    return seconds, nanoseconds
