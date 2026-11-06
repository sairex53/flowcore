"""Bounded owner-scoped workload discovery; registration grants no network privilege."""
import hashlib
import os
import re
import time
from collections import deque
from containers import cgroup, inspect_container
from benchmark import run

LABEL = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.@:-]{0,127}')


class Workloads:
    def __init__(self):
        self.rows = {}
        self.events = deque(maxlen=128)

    def register(self, uid, pid=None, service=None, container=None, unit=None):
        if service is not None and (not isinstance(service,str) or not LABEL.fullmatch(service)):
            raise ValueError('invalid service name')
        if container is not None and unit is not None:
            raise ValueError('choose container or systemd unit')
        metadata = {}
        if container is not None:
            if os.environ.get('CONTAINER_HOST') or os.environ.get('CONTAINER_CONNECTION'):
                raise ValueError('remote Podman discovery cannot identify a local process')
            item = inspect_container(container)
            pid = item['pid']
            metadata = dict(container_id=item['id'],container_name=item['name'])
        if unit is not None:
            if not isinstance(unit,str) or not LABEL.fullmatch(unit) or not unit.endswith('.service'):
                raise ValueError('invalid systemd service unit')
            args = ['systemctl']
            if uid != 0:
                args.append('--user')
            text = run([*args,'show',unit,'--property=Id,MainPID,ControlGroup','--no-pager'])
            values = dict(line.split('=',1) for line in text.splitlines() if '=' in line)
            pid = int(values.get('MainPID','0'))
            metadata = dict(systemd_unit=values.get('Id'),systemd_cgroup=values.get('ControlGroup'))
        identity = cgroup(pid)
        if uid != 0 and identity['uid'] != uid:
            raise PermissionError('workload belongs to another UID')
        if unit is not None and metadata['systemd_cgroup'] != identity['path']:
            raise ValueError('systemd process and cgroup disagree; retry discovery')
        key = hashlib.sha256(repr((pid,identity)).encode()).hexdigest()[:32]
        if key not in self.rows and len(self.rows) >= 128:
            raise ValueError('workload capacity exhausted')
        if key not in self.rows and sum(row['owner_uid']==uid for row in self.rows.values()) >= 32:
            raise ValueError('owner workload quota exhausted')
        old = self.rows.get(key)
        if old and old['owner_uid'] != uid:
            raise PermissionError('workload registration already owned')
        self.rows[key] = dict(id=key,pid=pid,identity=identity,owner_uid=uid,service=service,
                             metadata=metadata,state='active',registered_ns=time.monotonic_ns(),
                             network_authority=False)
        return self.rows[key]

    def refresh(self):
        for key,row in self.rows.items():
            if row['state'] != 'active':
                continue
            try:
                actual = cgroup(row['pid'])
            except (OSError,ValueError,ProcessLookupError):
                actual = None
            if actual != row['identity']:
                row['state'] = 'stale'
                row['event'] = 'exit_or_identity_change'
                self.events.append(dict(id=key,event=row['event'],time_ns=time.monotonic_ns(),
                                        owner_uid=row['owner_uid']))

    def view(self, uid):
        self.refresh()
        return dict(workloads=[row for row in self.rows.values() if row['owner_uid']==uid],
                    events=[row for row in self.events if row['owner_uid']==uid],
                    scope='explicit authorized registrations; no automatic network control')

    def forget(self, uid, key):
        if key not in self.rows:
            raise ValueError('unknown workload')
        if self.rows[key]['owner_uid'] != uid:
            raise PermissionError('workload belongs to another owner')
        del self.rows[key]
        return dict(removed=key)
