"""Bounded strict JSON decoding and explicit daemon operation authorization."""
import json
import math
import os
import socket
import stat
from service_config import unique

READ = frozenset(('ops_status','ops_validate','ops_diagnostics','status','lab_status','interfaces','observe','health','metrics','events','connections',
                  'congestion','inspect','bottleneck_inspect','capacity_estimate','qos_direction','path_diagnostics','diagnose','incident_list','incident_inspect','incident_export','policies','intent_list','intent_inspect','controller_status',
                  'policy_history','policy_explain','platform_interfaces','platform_policies',
                  'platform_resolve','platform_container_status','lifecycles','workload_list','platform_workload_classifiers'))


def decode(raw):
    def unsupported(value):
        raise ValueError(f'non-finite JSON number: {value}')
    try:
        value = json.loads(raw,object_pairs_hook=unique,parse_constant=unsupported)
    except RecursionError as error:
        raise ValueError('JSON nesting exceeds limit') from error
    if not isinstance(value,dict) or not isinstance(value.get('op'),str) or not 1<=len(value['op'])<=48:
        raise ValueError('request must contain a bounded operation name')
    stack = [(value,0)]
    nodes = 0
    while stack:
        item,depth = stack.pop()
        nodes += 1
        if nodes>4096 or depth>24:
            raise ValueError('JSON shape exceeds limits')
        if isinstance(item,float) and not math.isfinite(item):
            raise ValueError('non-finite JSON number')
        if isinstance(item,dict):
            stack.extend((child,depth+1) for child in item.values())
        elif isinstance(item,list):
            stack.extend((child,depth+1) for child in item)
    return value


def authorize(op, readonly, uid, owner):
    if (uid!=owner and not (uid==0 and op in READ)) or (readonly and op not in READ):
        raise PermissionError('operation forbidden by local daemon policy')


def private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError(f'expected private daemon-owned directory: {path}')


def private_lock(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise ValueError(f'expected private regular lock file: {path}')
        return os.fdopen(fd, 'a')
    except BaseException:
        os.close(fd)
        raise


def notify(message):
    target = os.environ.get('NOTIFY_SOCKET')
    if not target:
        return
    if target.startswith('@'):
        target = '\0'+target[1:]
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            sock.sendto(message.encode(),target)
    except OSError:
        pass
