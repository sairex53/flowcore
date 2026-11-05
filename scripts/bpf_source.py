"""Own the optional libbpf collector and its bounded pipe transport."""
import json
import ipaddress
import re
import os
import subprocess
import time
from pathlib import Path

from paths import BINDIR, BPF_DATA, BPF_MARKER


HOOKS = frozenset(('tcp_state','tcp_retransmit','tcp_reset'))


def validate_record(row):
    if not isinstance(row,dict) or type(row.get('version')) is not int or row['version'] != 1:
        raise ValueError('unsupported BPF transport version')
    kind = row.get('type')
    if kind == 'ready':
        for key in ('hooks','unavailable_hooks'):
            values = row.get(key)
            if (not isinstance(values,list) or len(values)>len(HOOKS)
                    or any(not isinstance(value,str) or value not in HOOKS for value in values)
                    or len(set(values)) != len(values)):
                raise ValueError('invalid hook status')
        if set(row['hooks']) & set(row['unavailable_hooks']):
            raise ValueError('conflicting hook status')
        if not row['hooks'] or row.get('mechanism') != 'raw_tracepoint':
            raise ValueError('unsupported attachment mechanism')
    elif kind == 'loss':
        if any(type(row.get(key,0)) is not int or not 0<=row.get(key,0)<2**64 for key in ('kernel','transport','decode')) or any(key not in row for key in ('kernel','transport')):
            raise ValueError('invalid loss counters')
    elif kind == 'tcp':
        bounds = dict(time_ns=2**64, lifetime_ns=2**64, flow_id=2**64, netns=2**32,
                      sport=65536,dport=65536,old=13,new=13,origin_state=13,kind=4)
        if any(type(row.get(key)) is not int or not 0<=row[key]<upper for key,upper in bounds.items()):
            raise ValueError('invalid TCP trace value')
        if row['kind'] not in (1,2,3) or type(row.get('family')) is not int or row['family'] not in (2,10):
            raise ValueError('invalid event type or family')
        if not row['time_ns'] or not row['netns'] or row['lifetime_ns']>row['time_ns']:
            raise ValueError('invalid event clock or namespace')
        if not isinstance(row.get('cookie'),str) or not re.fullmatch('[0-9a-f]{16}',row['cookie']):
            raise ValueError('invalid socket cookie')
        for field in ('local_address','remote_address'):
            value = ipaddress.ip_address(row[field])
            if value.version != (4 if row['family']==2 else 6):
                raise ValueError('trace address family mismatch')
    else:
        raise ValueError('unsupported BPF transport record')
    return row


class BpfSource:
    def __init__(self, mode):
        self.mode = mode
        self.proc = None
        self.log = None
        self.pending = b''
        self.reason = 'no lab'
        self.hooks = []
        self.unavailable_hooks = []
        self.loss = {'kernel': None, 'transport': None, 'decode':None}
        self.parse_drops = 0
        self.events = 0
        self.started = 0

    def start(self, namespace_fds, workspace):
        self.close()
        self.unavailable_hooks = []
        self.loss = {'kernel': None, 'transport': None, 'decode':None}
        self.events = self.parse_drops = 0
        self.pending = b''
        self.started = 0
        binary = BINDIR/'flowcore-bpf'
        obj = BPF_DATA/'observe.bpf.o'
        marker = BPF_MARKER
        if self.mode == 'off':
            self.reason = 'disabled by configuration'
        elif not Path('/sys/kernel/btf/vmlinux').is_file():
            self.reason = 'kernel BTF unavailable'
        elif not marker.is_file() or marker.read_text().strip() != '1' or not binary.is_file() or not obj.is_file():
            self.reason = 'CO-RE artifacts unavailable'
        else:
            self.log = (workspace / 'observer-bpf.log').open('w+')
            try:
                self.proc = subprocess.Popen([binary, obj, *(str(os.fstat(fd).st_ino)
                                                              for fd in namespace_fds)],
                                             stdout=subprocess.PIPE, stderr=self.log)
                os.set_blocking(self.proc.stdout.fileno(), False)
                self.started = time.monotonic()
                self.reason = 'waiting for attach'
            except OSError as error:
                self.reason = str(error)
                self.close()

    def close(self):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
            self.proc.stdout.close()
            self.proc = None
        if self.log:
            self.log.close()
            self.log = None
        self.hooks = []

    def fd(self):
        return self.proc.stdout.fileno() if self.proc else None

    def drain(self):
        rows = []
        if not self.proc:
            return rows
        try:
            data = os.read(self.fd(), 65536)
        except BlockingIOError:
            data = None
        except OSError as error:
            self.reason = f'BPF pipe read failed: {error}'
            self.close()
            return rows
        if data:
            lines = (self.pending + data).split(b'\n')
            self.pending = lines.pop()
            if len(self.pending) > 1024:
                self.parse_drops += 1
                self.pending = b''
            for line in lines:
                try:
                    if len(line)>1024:
                        raise ValueError('BPF record exceeds limit')
                    row = validate_record(json.loads(line))
                    if row['type'] == 'ready':
                        self.hooks = row['hooks']
                        self.unavailable_hooks = row['unavailable_hooks']
                        self.reason = None
                    elif row['type'] == 'loss':
                        self.loss = {'kernel': row['kernel'], 'transport': row['transport'], 'decode':row.get('decode',0)}
                    elif row['type'] == 'tcp':
                        rows.append(row)
                        self.events += 1
                except (ValueError, TypeError, KeyError, RecursionError):
                    self.parse_drops += 1
        if data == b'' or (not self.hooks and time.monotonic() - self.started > 5):
            self.log.seek(0)
            detail = self.log.read(4096).strip()
            self.reason = detail or 'BPF helper exited or attach timed out'
            self.close()
        return rows

    def status(self):
        return dict(active=bool(self.hooks), hooks=self.hooks, unavailable_hooks=self.unavailable_hooks,
                    mechanism='raw_tracepoint' if self.hooks else None, transport_version=1,
                    ring_capacity_bytes=262144, kernel_lifecycle_map_entries=4096, fallback_reason=self.reason,
                    btf=Path('/sys/kernel/btf/vmlinux').is_file(), events_processed=self.events,
                    kernel_reserve_drops=self.loss['kernel'], transport_drops=self.loss['transport'],
                    kernel_decode_drops=self.loss.get('decode'),
                    parse_drops=self.parse_drops,
                    processing_events_per_second=self.events / max(.001, time.monotonic() - self.started)
                    if self.started else None)
