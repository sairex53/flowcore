"""Own the optional libbpf collector and its bounded pipe transport."""
import json
import os
import subprocess
import time
from pathlib import Path

from paths import BINDIR, BPF_DATA, BPF_MARKER


class BpfSource:
    def __init__(self, mode):
        self.mode = mode
        self.proc = None
        self.log = None
        self.pending = b''
        self.reason = 'no lab'
        self.hooks = []
        self.loss = {'kernel': None, 'transport': None}
        self.parse_drops = 0
        self.events = 0
        self.started = 0

    def start(self, holders, workspace):
        self.close()
        self.loss = {'kernel': None, 'transport': None}
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
                self.proc = subprocess.Popen([binary, obj, *(str(os.stat(f'/proc/{p.pid}/ns/net').st_ino)
                                                              for p in holders)],
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
            if len(self.pending) > 512:
                self.parse_drops += 1
                self.pending = b''
            for line in lines:
                try:
                    row = json.loads(line)
                    if row['type'] == 'ready':
                        self.hooks = row['hooks']
                        self.reason = None
                    elif row['type'] == 'loss':
                        self.loss = {'kernel': row['kernel'], 'transport': row['transport']}
                    elif row['type'] == 'tcp':
                        rows.append(row)
                        self.events += 1
                except (ValueError, TypeError, KeyError):
                    self.parse_drops += 1
        if data == b'' or (not self.hooks and time.monotonic() - self.started > 5):
            self.log.seek(0)
            detail = self.log.read(4096).strip()
            self.reason = detail or 'BPF helper exited or attach timed out'
            self.close()
        return rows

    def status(self):
        return dict(active=bool(self.hooks), hooks=self.hooks, fallback_reason=self.reason,
                    btf=Path('/sys/kernel/btf/vmlinux').is_file(), events_processed=self.events,
                    kernel_reserve_drops=self.loss['kernel'], transport_drops=self.loss['transport'],
                    parse_drops=self.parse_drops,
                    processing_events_per_second=self.events / max(.001, time.monotonic() - self.started)
                    if self.started else None)
