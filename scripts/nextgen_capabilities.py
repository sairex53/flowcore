"""Read-only, scope-qualified discovery for the integrated experiment."""
import os
import socket
import subprocess
import time

from paths import BINDIR, BPF_DATA, BPF_MARKER


def read_only(operation):
    from cli import request
    try:
        return request(operation)
    except (OSError, ValueError, RuntimeError):
        return {}


def mptcp_loopback(peer_protocol):
    from multipath import PROTOCOL, _tcp_is_mptcp

    try:
        with (socket.socket(socket.AF_INET, socket.SOCK_STREAM, peer_protocol) as listener,
              socket.socket(socket.AF_INET, socket.SOCK_STREAM, PROTOCOL) as client):
            listener.settimeout(1)
            client.settimeout(1)
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            client.connect(listener.getsockname())
            accepted, _ = listener.accept()
            with accepted:
                accepted.settimeout(1)
                client.sendall(b'F')
                transferred = accepted.recv(1) == b'F'
                negotiated, _ = _tcp_is_mptcp(client)
                return dict(connected=transferred, negotiated=negotiated)
    except OSError as error:
        return dict(connected=False, negotiated=None, errno=error.errno)


def report(fabric_config=None):
    from deadline_runtime import estimate
    from ecn_cli import inspect as ecn_inspect
    from ecn_policy import local
    from multipath import PROTOCOL, capabilities as multipath
    from pacing_capabilities import report as socket_report

    try:
        options = socket_report()['socket_options']
    except (OSError, RuntimeError, AttributeError):
        options = {}
    observed = read_only('observe')
    lab = read_only('inspect')
    now = time.monotonic_ns()

    def row(state, scope, evidence):
        return dict(state=state, scope=scope, evidence=evidence)

    def socket_option(name):
        result = options.get(name)
        return row('verified' if result and result['supported'] else 'unavailable' if result else 'unknown',
                   'local probe socket', result)

    namespaces = lab.get('namespaces', [])
    classes = lab.get('tc', {}).get('traffic_classes', {})
    qdisc = next((q for q in lab.get('tc', {}).get('qdisc', []) if q.get('root')), None)
    app = any(service.get('application', {}).get('samples', 0)
              for window in observed.get('windows', {}).values()
              for service in window.get('application', {}).values())
    try:
        bpf_enabled = BPF_MARKER.read_text().strip() == '1'
    except OSError:
        bpf_enabled = False
    bpf_built = (bpf_enabled and (BINDIR/'flowcore-flow-engine').is_file()
                 and (BPF_DATA/'flow_engine.bpf.o').is_file())
    ecn = local().get('tcp_ecn')
    try:
        negotiation = ecn_inspect(limit=64)['negotiation']
    except (OSError, ValueError, RuntimeError):
        negotiation = {}
    negotiated = any(negotiation.get(mode, 0) for mode in ('classic', 'accurate'))
    try:
        deadline = estimate(dict(deadline_ns=now-1, egress_bytes=1, processing_us=0,
                                 connect_us=0, remote_us=0, reliability=0, expiration=0), {}, now)
        deadline_verified = deadline['reason'] == 'expired'
    except (OSError, AttributeError):
        deadline_verified = False
    mptcp = multipath()
    mptcp_peer = mptcp_loopback(PROTOCOL) if mptcp['socket_supported'] else {}
    tcp_peer = mptcp_loopback(socket.IPPROTO_TCP) if mptcp['socket_supported'] else {}
    mptcp_verified = mptcp_peer.get('connected') and mptcp_peer.get('negotiated') is True
    fabric = row('not_probed', 'operator-enrolled peer', 'Supply --fabric-config for a status RPC')
    if fabric_config:
        from types import SimpleNamespace
        from fabric_cli import dispatch
        try:
            dispatch(SimpleNamespace(action='status', config=fabric_config))
            fabric = row('verified', 'configured coordinator', 'authenticated status RPC')
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            fabric = row('unavailable', 'configured coordinator', 'authenticated status RPC failed')

    return dict(schema=1, kernel=os.uname().release, sampled_ns=now, capabilities={
        'traffic_control': row('verified' if qdisc and namespaces else 'unknown', 'daemon-owned lab',
                               dict(root=qdisc.get('kind') if qdisc else None, classes=sorted(classes))),
        'network_namespaces': row('verified' if len(namespaces) == 2 else 'available',
                                  'daemon-owned lab' if namespaces else 'current process',
                                  'owned namespace readback' if namespaces else 'current namespace handle exists'),
        'socket_pacing': socket_option('SO_MAX_PACING_RATE'),
        'tcp_info': socket_option('TCP_INFO'),
        'ecn': row('verified' if negotiated else 'available' if ecn is not None else 'unknown',
                   'current network namespace', dict(sysctl=ecn, negotiation=negotiation)),
        'ebpf_classification': row('available' if bpf_built else 'unavailable', 'local build artifacts',
                                   'TCX attachment and classifier require separate owned-scope verification'),
        'mptcp': row('verified' if mptcp_verified else 'available' if mptcp['socket_supported'] else 'unavailable',
                     'local loopback pair', dict(netlink=mptcp['netlink_interfaces'],
                         socket_supported=mptcp['socket_supported'], mptcp_peer=mptcp_peer, tcp_peer=tcp_peer)),
        'application_slo': row('verified' if app else 'unknown', 'daemon observer',
                               'application samples present' if app else 'no application sample observed'),
        'deadline': row('verified' if deadline_verified else 'unavailable', 'native estimator',
                        'expired request fixture with no network evidence'),
        'distributed_coordination': fabric,
    })
