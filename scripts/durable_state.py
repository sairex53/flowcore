"""Private bounded state files with fsync+rename; no kernel transaction claims."""
import json
import os
import secrets
import stat
from service_config import unique

class DurabilityUncertain(OSError):
    """Replacement committed, but directory durability could not be confirmed."""


def read_json(path,limit=524288,*,trusted_owners=()):
    fd=os.open(path,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0,os.geteuid(),*trusted_owners) or info.st_mode&0o022 or info.st_nlink!=1:
            raise PermissionError('state/configuration must be an owned non-writable regular file')
        data=stream.read(limit+1)
        if len(data)>limit:raise ValueError('state/configuration exceeds size limit')
    return json.loads(data,object_pairs_hook=unique,parse_constant=lambda value:(_ for _ in ()).throw(ValueError('non-finite number')))


def atomic_json(path,value,on_commit=None):
    directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC|os.O_NOFOLLOW)
    name='.state-'+secrets.token_hex(12)
    try:
        info=os.fstat(directory)
        if info.st_uid!=os.geteuid() or info.st_mode&0o077:
            raise PermissionError('state parent must be private and owned')
        try:
            old=os.stat(path.name,dir_fd=directory,follow_symlinks=False)
        except FileNotFoundError:
            old=None
        if old and (not stat.S_ISREG(old.st_mode) or old.st_nlink!=1 or old.st_uid!=os.geteuid() or old.st_mode&0o077):
            raise PermissionError('refusing unsafe state replacement')
        payload=(json.dumps(value,allow_nan=False)+'\n').encode()
        if len(payload)>524288:raise ValueError('state exceeds 512 KiB')
        fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_CLOEXEC|os.O_NOFOLLOW,0o600,dir_fd=directory)
        with os.fdopen(fd,'wb') as stream:
            stream.write(payload);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path.name,src_dir_fd=directory,dst_dir_fd=directory)
        if on_commit:on_commit()
        try:
            os.fsync(directory)
        except OSError as error:
            raise DurabilityUncertain('replacement committed; directory durability unconfirmed') from error
    finally:
        try:os.unlink(name,dir_fd=directory)
        except FileNotFoundError:pass
        os.close(directory)
