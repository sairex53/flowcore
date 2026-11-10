"""Publish a bounded helper observation using only the invoking user's authority."""
from contextlib import contextmanager
import json
import os
import re
import secrets
import stat


@contextmanager
def destination(path, name):
    if os.geteuid() == 0:
        raise PermissionError('result publishing requires an unprivileged account')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\.json', name):
        raise ValueError('expected a simple .json result filename')
    parts = os.fspath(path).split('/')
    if len(parts) < 2 or parts[0] or any(part in ('', '.', '..') for part in parts[1:]):
        raise ValueError('expected a canonical absolute result directory')
    uid = os.geteuid()
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, part in enumerate(parts[1:], 1):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=directory)
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            if index == len(parts)-1:
                if info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
                    raise PermissionError('result directory must be caller-owned and mode 0700')
            elif (info.st_uid not in (0, uid)
                  or info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)):
                raise PermissionError('untrusted result ancestor')
        try:
            os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError('result already exists')
        yield directory
    finally:
        os.close(directory)


def publish(directory, name, result):
    data = (json.dumps(result, sort_keys=True, allow_nan=False)+'\n').encode()
    if len(data) > 16384:
        raise ValueError('result exceeds publication limit')
    temporary = '.flowcore-'+secrets.token_hex(16)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        os.fsync(directory)
    finally:
        os.unlink(temporary, dir_fd=directory)
