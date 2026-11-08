#!/usr/bin/env python3
"""Discover TCP socket options, congestion algorithms and optional BPF program types."""
import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import subprocess

from paths import TC
from pacing_workload import checked, library

OPTIONS = ('SO_MAX_PACING_RATE', 'SO_PRIORITY', 'SO_MARK_read', 'SO_MARK_write',
           'TCP_NOTSENT_LOWAT', 'TCP_CONGESTION', 'TCP_INFO', 'SO_COOKIE')


def command(argv):
    try:
        output = subprocess.run(argv, check=True, capture_output=True, text=True, timeout=15)
        return json.loads(output.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return {'status': 'unavailable', 'error': str(error)}


def report(probe_bpf=False):
    values = (ctypes.c_int*len(OPTIONS))()
    probe = library().fc_pacing_capabilities
    probe.argtypes = [ctypes.POINTER(ctypes.c_int)]
    probe.restype = ctypes.c_int
    checked(probe(values), 'TCP option probe')
    result = {'schema': 1, 'kernel': os.uname().release, 'euid': os.geteuid(),
              'socket_options': {name: {'supported': error == 0, 'error': errno.errorcode.get(error)}
                                 for name, error in zip(OPTIONS, values)}}
    sysctl = Path('/proc/sys/net/ipv4')
    algorithms = {}
    for name, leaf in (('current', 'tcp_congestion_control'), ('unprivileged', 'tcp_allowed_congestion_control'),
                       ('registered', 'tcp_available_congestion_control')):
        try:
            algorithms[name] = (sysctl/leaf).read_text().split()
        except OSError:
            algorithms[name] = None
    result['congestion_control'] = algorithms
    qdisc = command([str(TC), '-j', 'qdisc', 'show'])
    result['fq_on_visible_interface'] = any(row.get('kind') == 'fq' for row in qdisc) if isinstance(qdisc, list) else None
    config = Path('/boot')/('config-'+os.uname().release)
    try:
        settings = dict(line.split('=', 1) for line in config.read_text().splitlines() if line.startswith('CONFIG_'))
    except OSError:
        settings = {}
    result['kernel_config'] = {name: settings.get(name) for name in
        ('CONFIG_NET_SCH_FQ', 'CONFIG_BPF_SYSCALL', 'CONFIG_CGROUP_BPF', 'CONFIG_TCP_CONG_BBR')}
    result['bpf'] = {'status': 'not_probed'}
    if probe_bpf:
        values = command(['/usr/bin/bpftool', '-j', 'feature', 'probe', 'kernel'])
        if 'program_types' not in values:
            result['bpf'] = values
        else:
            names = ('sock_ops', 'cgroup_sock', 'cgroup_sock_addr', 'cgroup_sockopt', 'cgroup_skb')
            result['bpf'] = {'status': 'probed', 'program_types': {
                name: values['program_types'].get(f'have_{name}_prog_type') for name in names},
                'sock_ops_helpers': [name for name in values.get('helpers', {}).get('sock_ops_available_helpers', [])
                                     if name in ('bpf_getsockopt', 'bpf_setsockopt', 'bpf_get_socket_cookie')]}
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe-bpf', action='store_true', help='explicitly run privileged kernel feature probes')
    args = parser.parse_args()
    print(json.dumps(report(args.probe_bpf), indent=2))
