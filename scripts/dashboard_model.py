"""Versioned projections from one flowcored event-loop snapshot."""
import re
import time
from contextlib import contextmanager
import socket

from causal_diagnostics import replay
from service_config import keys

MONITOR_OPS = frozenset(('monitor_snapshot', 'monitor_incident'))
ABSTRACT_NAME = re.compile(r'[a-zA-Z0-9_.-]{1,64}', re.ASCII)


def abstract_address(name):
    if not isinstance(name, str) or not ABSTRACT_NAME.fullmatch(name):
        raise ValueError('invalid abstract monitoring socket name')
    return '\0' + name


@contextmanager
def monitor_listener(name):
    if name is None:
        yield None
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        server.bind(abstract_address(name))
        server.listen(8)
        yield server


def fields(row, names):
    return {key: row.get(key) for key in names}


def rate(numerator, denominator):
    return numerator / denominator if denominator else None


def milliseconds(value):
    return value / 1000 if type(value) in (float, int) else None


def service_row(name, row, window, settings):
    source = row['application']
    ok = source['ok']
    errors = source['timeout'] + source['failure']
    attempts = ok + errors
    target = settings.get('slo_ms')
    p99 = milliseconds(source['p99_us'])
    return dict(id=name, name=name, traffic_class=row['traffic_class'], window_seconds=window,
                samples=source['samples'], attempts=attempts, successes=ok, errors=errors,
                deadline_reported=source['deadline_reported'], deadline_misses=source['deadline_miss'],
                p50_ms=milliseconds(source['p50_us']), p95_ms=milliseconds(source['p95_us']),
                p99_ms=p99, target_ms=target, error_rate=rate(errors, attempts),
                deadline_miss_rate=rate(source['deadline_miss'], source['deadline_reported']),
                requests_per_second=attempts/window if attempts else None,
                above_target=p99 > target if p99 is not None and target is not None else None)


DIRECTION = ('device', 'qdisc', 'controllable', 'capacity_kbit', 'capacity_source',
             'capacity_uncertainty', 'observed_peak_kbit', 'throughput_kbit', 'utilization',
             'idle_headroom_kbit', 'backlog_bytes', 'dropped_packets', 'pressure', 'byte_source')


def interface_row(scope, link, diagnostic):
    name = link['name']
    path = diagnostic or {}
    directions = {key: fields(value, DIRECTION) for key, value in path.get('directions', {}).items()}
    return dict(id=f'{scope}:{name}', name=name, scope=scope, directions=directions,
                **fields(link, ('kind', 'up', 'carrier', 'mtu', 'speed_mbps')))


def bottleneck(row, diagnostic):
    if not diagnostic:
        return dict(id=row['id'], interface=row['name'], scope=row['scope'], state='UNKNOWN',
                    classification='UNKNOWN', confidence='insufficient', reason='no eligible observation',
                    action='OBSERVE', directions=row['directions'], transport=None, application=[])
    return dict(id=row['id'], interface=row['name'], scope=row['scope'], directions=row['directions'],
                **fields(diagnostic, ('state', 'classification', 'confidence', 'reason', 'action',
                                      'generation', 'sample_age_seconds', 'interval_seconds', 'transport',
                                      'application', 'capacity_reductions', 'last_capacity_reduction')))


def decision(row):
    value = fields(row, ('time_unix', 'time_ns', 'generation', 'controller_session', 'result',
                         'action', 'reason', 'service', 'previous', 'proposed', 'allocation',
                         'expected', 'applied', 'comparable', 'degraded', 'compensated',
                         'feasible', 'conflict', 'bottleneck_gate', 'pressure', 'prediction'))
    timing = row.get('timing')
    value['timing'] = fields(timing, ('transaction', 'verified_ns', 'apply_ns', 'verify_ns')) if isinstance(timing, dict) else None
    return value


def snapshot(service, window):
    observer = service.observer
    selected = min(observer.cfg['windows'], key=lambda seconds: (abs(seconds-window), -seconds))
    source = observer.window(time.monotonic_ns(), selected)
    inventory = service.inventory
    interfaces, classes, diagnostics = [], [], []
    for link in inventory.view.get('interfaces', [])[:64]:
        monitor = service.diagnostics.host.get(link['name'])
        info = monitor.view() if monitor else None
        row = interface_row('host', link, info)
        interfaces.append(row)
        diagnostics.append(bottleneck(row, info))
    if service.lab:
        info = service.diagnostics.lab.view(max_age=service.diagnostics.max_age())
        link = dict(name='fc0', kind='veth', up=None, carrier=None)
        try:
            lab_links = service.lab.network(0).get('interfaces', [])
            link = next((item for item in lab_links if item['name'] == 'fc0'), link)
        except (OSError, ValueError, RuntimeError):
            pass
        row = interface_row('lab', link, info)
        interfaces.append(row)
        diagnostics.append(bottleneck(row, info))
        for direction, values in info.get('directions', {}).items():
            for name, item in values.get('classes', {}).items():
                classes.append(dict(id=f'lab:fc0:{direction}:{name}', name=name, direction=direction,
                    interface_id=row['id'], **fields(item, ('served_kbit', 'backlog_bytes',
                    'dropped_packets', 'queue_accounted_demand_kbit', 'guaranteed_kbit'))))
    controller = service.controller
    return dict(schema=1, captured_ms=time.time_ns()//1_000_000, clock='unix_ms',
                monotonic_ns=time.monotonic_ns(), scope=observer.scope, window_seconds=selected,
                status=fields(service.status(), ('healthy', 'monitor_only', 'lab_up', 'policy', 'uptime_seconds')),
                quality=dict(enabled=observer.cfg['enabled'], truncated=observer.truncated,
                             collector_errors=sorted(observer.errors), inventory_available=not inventory.error,
                             sample_age_seconds=(time.monotonic_ns()-observer.sampled_ns)/1e9
                             if observer.sampled_ns is not None else None),
                interfaces=interfaces, classes=classes, bottlenecks=diagnostics,
                services=[service_row(name, row, selected, observer.cfg['services'][name])
                          for name, row in sorted(source['application'].items())[:64]],
                controller=dict(state=controller.state.name, state_reason=controller.state.reason,
                                mode=controller.mode, strategy=controller.strategy, actions=controller.actions,
                                allocation=controller.current, allocation_verified=controller.allocation_confirmed
                                and not controller.error and controller.mode != 'off',
                                reversals=controller.reversals, error=controller.error,
                                pending=controller.pending is not None),
                policy_history=[decision(row) for row in reversed(controller.history)],
                incidents=service.incident.command({'op': 'incident_list'}),
                link_events=list(reversed(service.events))[:32])


def request(service, message):
    op = message['op']
    if op == 'monitor_snapshot':
        keys(message, ('op', 'window'))
        window = message['window']
        if type(window) is not int or not 1 <= window <= 3600:
            raise ValueError('monitoring window out of range')
        return snapshot(service, window)
    if op == 'monitor_incident':
        keys(message, ('op', 'id'))
        bundle = service.incident.command({'op': 'incident_export', 'id': message['id']})
        report = replay(bundle)
        events = sorted(bundle['events'], key=lambda row: (row['time_ns'], row['seq']))
        return dict(id=bundle['id'], origin_ns=events[0]['time_ns'], events=events, report=report)
    raise PermissionError('operation not available on the monitoring socket')
