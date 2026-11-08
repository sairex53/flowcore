"""Read-only ECN inspection and explicit owned-lab execution."""
import collections
import json
import os
from pathlib import Path
import re
import subprocess
import time

from ecn_feedback import aqm_counters, mode, pairs
import ecn_policy
from tcp_diag import connections

METRICS = ('ecn_negotiated', 'received_ect', 'ecn_mode', 'accecn_option_state', 'accecn_fail_flags',
           'delivered_segments', 'delivered_ce_segments', 'received_ce_segments',
           'accecn_received_ce_bytes', 'accecn_delivered_ce_bytes', 'accecn_delivered_ect0_bytes',
           'accecn_delivered_ect1_bytes', 'total_retrans_segments', 'lost_segments', 'rtt_us',
           'delivery_rate_bytes_per_second')


def add_commands(commands):
    sub = commands.add_parser('ecn', help='inspect ECN or run an isolated experiment').add_subparsers(dest='action', required=True)
    cap = sub.add_parser('capabilities')
    cap.add_argument('--probe', action='store_true', help='run disposable privileged negotiation and marking probes')
    for name in ('inspect', 'monitor'):
        p = sub.add_parser(name)
        p.add_argument('--limit', type=int, default=128)
        p.add_argument('--interface')
        if name == 'monitor':
            p.add_argument('--interval', type=float, default=1)
            p.add_argument('--count', type=int, default=5)
    lab = sub.add_parser('lab')
    lab.add_argument('profile', type=Path)
    lab.add_argument('--check', action='store_true', help='validate profile and list prerequisites without running')


def qdiscs(interface=None):
    command = [str(ecn_policy.TC), '-j', '-s', 'qdisc', 'show']
    if interface is not None:
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', interface) or interface.startswith('-'):
            raise ValueError('invalid interface name')
        command.extend(('dev', interface))
    try:
        text = subprocess.run(command, check=True, capture_output=True, text=True, timeout=3).stdout
        return [dict(interface=row.get('dev'), kind=row['kind'], handle=row.get('handle'),
                     parent=row.get('parent'), options=row.get('options'), counters=aqm_counters(row))
                for row in json.loads(text) if row.get('kind') in ('fq_codel', 'cake')]
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def inspect(limit=128, interface=None):
    rows, truncated = connections(limit)
    uid = os.geteuid()
    found = []
    for row in rows:
        if row['state'] != 'established' or (uid and uid != row['uid']):
            continue
        info = row['tcp_info']
        found.append(dict(cookie=row['cookie'], negotiation=mode(info),
                          **{key: info[key] for key in METRICS if key in info}))
    return dict(schema=1, time_ns=time.monotonic_ns(), truncated=truncated, sockets=found,
                negotiation=dict(collections.Counter(row['negotiation'] for row in found)),
                qdiscs=qdiscs(interface), association='socket and interface views are independent')


def capabilities(probe=False):
    sysctl = ecn_policy.local()
    boot = Path('/boot')/('config-'+os.uname().release)
    names = ('CONFIG_NET_SCH_FQ_CODEL', 'CONFIG_NET_SCH_CAKE', 'CONFIG_NET_SCH_DUALPI2',
             'CONFIG_TCP_CONG_DCTCP', 'CONFIG_TCP_CONG_PRAGUE')
    try:
        config = dict(line.split('=', 1) for line in boot.read_text().splitlines() if line.startswith('CONFIG_'))
    except OSError:
        config = {}
    algorithms = (sysctl.get('tcp_available_congestion_control') or '').split()
    result = dict(schema=1, kernel=os.uname().release, namespace=os.stat('/proc/self/ns/net').st_ino,
                  configured=sysctl, registered_algorithms=algorithms,
                  configured_kernel_features={key: config.get(key) for key in names}, qdiscs=qdiscs(),
                  dctcp_registered='dctcp' in algorithms,
                  accurate_options_exposed=sysctl['tcp_ecn_option'] is not None,
                  l4s=dict(endpoint_candidate_registered='prague' in algorithms, validated=False))
    if probe:
        from ecn_probe import run
        result['negotiation_probes'] = run()
    return result


def signals(artifact=None):
    if artifact is None:
        view = inspect()
        return dict(schema=1, status='unpaired_snapshot', negotiation=view['negotiation'], qdiscs=view['qdiscs'],
                    reason='use ecn monitor for transport deltas or supply a scoped lab artifact')
    with artifact.open('rb') as source:
        data = source.read(2*1024*1024+1)
    if len(data) > 2*1024*1024:
        raise ValueError('ECN artifact exceeds 2 MiB')
    observations = []
    for line in data.splitlines():
        row = json.loads(line)
        if isinstance(row, dict) and isinstance(row.get('congestion'), dict):
            observations.append(dict(time_ns=row.get('time_ns'), decision=row.get('decision'),
                                     verified=row.get('verified'), signals=row['congestion']))
    return dict(schema=1, source='research_artifact', observations=observations)


def dispatch(args):
    if args.action == 'capabilities':
        result = capabilities(args.probe)
        print(json.dumps(result, indent=2))
        if args.probe and (not result['negotiation_probes']['host_unchanged'] or
                           any(row['status'] == 'invalid' for row in result['negotiation_probes']['cases'].values())):
            return 1
        return 0
    if args.action == 'inspect':
        print(json.dumps(inspect(args.limit, args.interface), indent=2))
        return 0
    if args.action == 'monitor':
        if not .25 <= args.interval <= 60 or not 1 <= args.count <= 600:
            raise ValueError('expected 0.25–60 second interval and 1–600 snapshots')
        previous, previous_truncated = {}, True
        for index in range(args.count):
            view = inspect(args.limit, args.interface)
            now = {row['cookie']: dict(row, total_retransmits=row.get('total_retrans_segments'))
                   for row in view['sockets'] if row['cookie']}
            view['sender_counter_deltas'] = None if previous_truncated or view['truncated'] else pairs(previous, now)
            previous, previous_truncated = now, view['truncated']
            print(json.dumps(view), flush=True)
            if index+1 < args.count:
                time.sleep(args.interval)
        return 0
    from research_manifest import load_manifest
    from research_report import report
    from research_runner import execute, exclusive
    manifest = load_manifest(args.profile)
    if 'ecn' not in manifest:
        raise ValueError('ECN lab requires a versioned ECN manifest')
    selected = ecn_policy.validate(manifest['ecn'])
    sysctls = ecn_policy.local()
    registered = (sysctls['tcp_available_congestion_control'] or '').split()
    missing = []
    if 'ecn-dctcp' in manifest['modes'] and 'dctcp' not in registered:
        missing.append('register the stock tcp_dctcp algorithm explicitly')
    if selected['tcp_ecn'] == 3 and sysctls['tcp_ecn_option'] is None:
        missing.append('Accurate ECN sysctls are unavailable')
    if args.check:
        print(json.dumps(dict(schema=1, valid=True, profile=selected,
                              modes=manifest['modes'], missing_prerequisites=missing), indent=2))
        return 0
    if missing:
        raise ValueError('; '.join(missing))
    with exclusive():
        directory, result = execute(manifest, label='ecn-lab')
        page = report(directory/'summary.json')
    invalid = sum(not row['valid'] for row in result['runs'])
    print(json.dumps(dict(artifact=str(directory), report=str(page), valid=len(result['runs'])-invalid,
                          invalid=invalid), indent=2))
    return int(bool(invalid))
