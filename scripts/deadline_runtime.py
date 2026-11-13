"""Typed admission queries and scoped evidence from existing FLOWCORE sampling."""
import ctypes as c
from functools import cache
import socket
import sys
import time

from paths import BINDIR
from service_config import CLASSES, keys, number

NANO = 1_000_000_000
PACING_BASELINE = 650_000
MISSING = 2**64-1
DECISIONS = ('ACCEPT', 'ACCEPT_WITH_RISK', 'DEFER', 'REJECT_INFEASIBLE', 'INSUFFICIENT_DATA')
REASONS = ('fits', 'tight', 'queue_pressure', 'expired', 'capacity_bound', 'missing_evidence',
           'offline', 'invalid', 'unauthorized', 'budget_exhausted', 'throttled')
QUALITY = ('none', 'limited', 'observed')
MODES = ('deadline-unaware', 'deadline-static', 'deadline-existing', 'deadline-admission',
         'deadline-adaptive', 'deadline-paced-static', 'deadline-concurrency-static',
         'deadline-overload-v2')
FIELDS = ('deadline_ns', 'egress_bytes', 'processing_us', 'connect_us', 'remote_us', 'reliability', 'expiration')
EVIDENCE = ('observed_ns', 'capacity_bytes_s', 'observed_rate_bytes_s', 'backlog_bytes',
            'rtt_p50_us', 'rtt_p95_us', 'transfer_p95_us', 'owned_egress', 'class_scoped', 'hard_burst_bytes')
U64 = c.c_uint64


def pacing_floor(manifest):
    capacities = [manifest['network']['bandwidth_kbit']]
    capacities.extend(fault['bandwidth_kbit'] for fault in manifest['faults'] if 'bandwidth_kbit' in fault)
    for event in manifest.get('events', ()):
        if 'bandwidth_kbit' in event['effects']:
            capacities.append(event['effects']['bandwidth_kbit'])
    return max(50_000, (max(capacities)*(60-manifest['static_critical_pct'])//100)*125)


def profile(value):
    version = value.get('version')
    base = ('version', 'tight_every', 'tight_deadline_ms', 'connect_reserve_us', 'defer_us')
    overload = ('response_bytes', 'processing_p50_us', 'processing_p95_us',
                'minimum_concurrency', 'maximum_concurrency', 'static_concurrency_limit',
                'maximum_admissions_per_second')
    keys(value, base + (overload if version == 2 else ()))
    number(version, 1, 2, True)
    number(value['tight_every'], 4, 100, True)
    number(value['tight_deadline_ms'], .001, 20)
    number(value['connect_reserve_us'], 0, 200_000, True)
    number(value['defer_us'], 0, 5000, True)
    if version == 2:
        number(value['response_bytes'], 0, 64*1024**2, True)
        number(value['processing_p50_us'], 0, 600_000_000, True)
        number(value['processing_p95_us'], value['processing_p50_us'], 600_000_000, True)
        number(value['minimum_concurrency'], 1, 1_000_000, True)
        number(value['maximum_concurrency'], value['minimum_concurrency'], 1_000_000, True)
        number(value['static_concurrency_limit'], 1, value['maximum_concurrency'], True)
        number(value['maximum_admissions_per_second'], 1, 1_000_000, True)
    return dict(value)


@cache
def native():
    library = c.CDLL(str(BINDIR/'libflowcore_intelligence.so'))
    fn = library.flowcore_deadline_estimate_v1
    fn.argtypes = [c.POINTER(U64), c.POINTER(U64), U64, c.POINTER(U64)]
    fn.restype = None
    return fn


def estimate(work, observation, now):
    def value(key, fields, default=MISSING):
        raw = fields.get(key)
        return int(raw) if type(raw) in (int, bool) and 0 <= raw < MISSING else default
    w = (U64*len(FIELDS))(*(value(key, work) for key in FIELDS))
    e = (U64*len(EVIDENCE))(*(value(key, observation, 0 if key in ('observed_ns', 'owned_egress', 'class_scoped') else MISSING)
                             for key in EVIDENCE))
    out = (U64*9)()
    native()(w, e, now, out)
    return dict(deadline_decision=out[0], deadline_reason=out[1], deadline_quality=out[2],
                decision=DECISIONS[out[0]], reason=REASONS[out[1]], quality=QUALITY[out[2]],
                remaining_ns=out[3], capacity_bound_us=out[4], queue_us=out[5],
                network_p50_us=out[6], network_p95_us=out[7], reserved_us=out[8])


def evidence(service, now):
    observer = service.observer
    if not service.lab:
        service.deadline_evidence_status = dict(available=False, reason='no_owned_lab')
        return {}
    if not observer.sampled_ns:
        service.deadline_evidence_status = dict(available=False, reason='no_observer_sample')
        return {}
    if observer.errors or observer.truncated:
        service.deadline_evidence_status = dict(available=False, reason='observer_quality_error',
                                                errors=observer.errors, truncated=observer.truncated)
        return {}
    tc = service.telemetry
    root = next((q for q in tc.get('qdisc', []) if q.get('root')), {})
    if root.get('kind') != 'htb' or root.get('handle') != '1:':
        service.deadline_evidence_status = dict(available=False, reason='unsupported_qdisc_root')
        return {}
    capacity = service.profile['bandwidth_kbit']*125
    classes = {row['handle']: row for row in tc.get('class', []) if row.get('class') == 'htb'}
    parent = classes.get('1:1') or classes.get('1:20', {})
    if parent.get('rate') != capacity or parent.get('ceil') != capacity:
        service.deadline_evidence_status = dict(available=False, reason='capacity_readback_mismatch',
            expected_bytes_s=capacity, observed_rate_bytes_s=parent.get('rate'),
            observed_ceil_bytes_s=parent.get('ceil'))
        return {}
    if not 0 <= now-observer.sampled_ns <= 2_500_000_000:
        service.deadline_evidence_status = dict(available=False, reason='stale_observation',
                                                age_ns=now-observer.sampled_ns)
        return {}
    window = observer.window(now, 2)
    rtt = window['rtt']
    backlog = {row['parent']: row.get('backlog') for row in tc.get('qdisc', []) if row.get('parent')}
    out = {}
    for cls, minor in CLASSES.items():
        handle = f'1:{minor}'
        class_row = classes.get(handle, {})
        scoped = ('1:1' in classes and handle in classes and
                  tc.get('traffic_classes', {}).get(cls, {}).get('handle') == handle)
        rate = window['queue'].get(cls, {}).get('bytes_per_second')
        guaranteed = class_row.get('rate') if type(class_row.get('rate')) is int else None
        ceiling = class_row.get('ceil') if type(class_row.get('ceil')) is int else None
        available = min(ceiling, max(guaranteed or 0, int(rate or 0))) if ceiling else None
        background = classes.get(f"1:{CLASSES['background']}", {})
        background_floor = background.get('rate') if type(background.get('rate')) is int else None
        fairness_verified = ('1:1' in classes and type(background_floor) is int and
                             background.get('class') == 'htb')
        out[cls] = dict(observed_ns=observer.sampled_ns, owned_egress=True, class_scoped=scoped,
                        capacity_bytes_s=capacity, observed_rate_bytes_s=int(rate) if rate is not None else None,
                        available_bytes_s=available, class_rate_bytes_s=guaranteed,
                        class_ceil_bytes_s=ceiling, backlog_bytes=backlog.get(handle),
                        rtt_p50_us=rtt.get('p50_us'), rtt_p95_us=rtt.get('p95_us'),
                        response_available_bytes_s=capacity, response_backlog_bytes=0,
                        response_path_verified=False, background_floor_bytes_s=background_floor,
                        background_goodput_bytes_s=window['queue'].get('background', {}).get('bytes_per_second'),
                        operator_fairness_verified=fairness_verified)
    service.deadline_evidence_status = dict(available=True, reason='class_scoped_readback',
        class_scoped={name: row['class_scoped'] for name, row in out.items()},
        observer_sample_ns=observer.sampled_ns)
    return out


def netns_cookie(sock):
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, getattr(socket, 'SO_NETNS_COOKIE', 71), 8)
        return int.from_bytes(raw, sys.byteorder) or None
    except OSError:
        return None


def lab_route_matches(service, lease, cookie, traffic_class):
    lab = service.lab
    identity = lease.get('socket_identities', {}).get(cookie)
    capability = lease.get('socket_capabilities', {}).get(cookie, {})
    if not lab or not identity or capability.get('protocol') != socket.IPPROTO_TCP or not capability.get('netns_cookie'):
        return False
    family, source, _, destination, port = identity
    peers = {socket.AF_INET: ('10.203.0.1', '10.203.0.2'),
             socket.AF_INET6: ('fd42:203::1', 'fd42:203::2')}
    policy = service.config['policies'].get(service.active, {})
    if (source, destination) != peers.get(family) or port != policy.get('classes', {}).get(traffic_class, {}).get('port'):
        return False
    expected_protocol = 'ipv6' if family == socket.AF_INET6 else 'ip'
    expected_class = f'1:{CLASSES[traffic_class]}'
    if not any(row.get('kind') == 'flower' and row.get('protocol') == expected_protocol and
               options.get('classid') == expected_class and
               options.get('keys', {}).get('dst_port') == port and
               options.get('keys', {}).get('ip_proto') == 'tcp'
               for row in service.telemetry.get('filter', []) for options in (row.get('options', {}),)):
        return False
    if not hasattr(lab, 'deadline_sender_netns_cookie'):
        try:
            with lab.enter(0), socket.socket() as sender:
                lab.deadline_sender_netns_cookie = netns_cookie(sender)
        except (OSError, RuntimeError):
            return False
    return lab.deadline_sender_netns_cookie == capability['netns_cookie']


def query(service, lease, work, now=None):
    now = time.monotonic_ns() if now is None else now
    keys(work, (*FIELDS, 'cookie'))
    number(work['deadline_ns'], 1, now+600*NANO, True)
    number(work['egress_bytes'], 0, 64*1024*1024, True)
    for key in ('processing_us', 'connect_us', 'remote_us'):
        if work[key] is not None:
            number(work[key], 0, 600_000_000, True)
    number(work['reliability'], 0, 1, True)
    number(work['expiration'], 0, 1, True)
    cookie = work['cookie']
    if cookie is not None:
        number(cookie, 1, 2**64-1, True)
        if cookie not in lease['cookies']:
            raise PermissionError('deadline query cookie is outside its authenticated lease')
    intent = service.controller.registry.rows[lease['name']]['intent']
    local = estimate(work, {}, now)
    if local['decision'] == 'REJECT_INFEASIBLE':
        return local
    credit, prior = lease.get('deadline_credit', (64.0, now))
    credit = min(64.0, credit+max(0, now-prior)*64/NANO)
    lease['deadline_credit'] = (max(0., credit-1), now)
    if credit < 1:
        return dict(local, deadline_reason=10, reason='throttled')
    # Service priority is operator-admitted, rather than a field in the query.
    cls = intent['priority']
    observation = evidence(service, now).get(cls, {}) if lab_route_matches(service, lease, cookie, cls) else {}
    return estimate(work, observation, now)
