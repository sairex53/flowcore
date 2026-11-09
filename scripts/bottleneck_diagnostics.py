"""Daemon adapters for the directional evidence engine."""
import time

from bottleneck import Bottleneck, direction, lab_directions, lab_sockets
from service_config import keys
from path_adaptation import advisory


class Diagnostics:
    def __init__(self, service):
        self.service = service
        self.lab = Bottleneck()
        self.host = {}

    def max_age(self):
        return max(1, 2.5*self.service.config['telemetry_seconds'])

    def lab_sample(self, tc, sampled_at):
        service = self.service
        observer = service.observer
        lab = service.lab
        if lab.topology_dirty:
            try:
                lab.validate()
            except (OSError, ValueError, RuntimeError):
                self.lab.reset('lab topology could not be verified')
                return
            lab.topology_dirty = False
        capacity = lab_directions(tc, egress_owned=True)
        windows = observer.window(time.monotonic_ns(), 5)['application'] if observer.cfg['enabled'] else {}
        reports = []
        for name, row in windows.items():
            app = row['application']
            traffic_class = row['traffic_class']
            port = service.config['policies'][service.active].get('classes', {}).get(traffic_class, {}).get('port')
            reports.append(dict(service=name, samples=app['samples'], p95_us=app['p95_us'], port=port,
                                target_us=observer.cfg['services'][name]['slo_ms']*1000,
                                **{'class': row['traffic_class']}))
        self.lab.observe(sampled_at, capacity, lab_sockets(observer.last_rows, lab.sender_index, lab.sender_addresses),
                         identity=lab.identity, application=reports, max_gap=self.max_age(),
                         socket_coverage=bool(observer.cfg['enabled'] and not observer.truncated
                                              and 'inet_diag' not in observer.errors))

    def host_sample(self):
        inventory = self.service.inventory
        if inventory.error:
            self.host.clear()
            return
        current = {}
        routes = frozenset(inventory.routes)
        for link in inventory.view.get('interfaces', [])[:64]:
            name, index = link['name'], link['index']
            monitor = self.host.get(name) or Bottleneck()
            current[name] = monitor
            medium = link['speed_mbps'] if link['kind'] == 'ethernet' else None
            nominal = medium*1000 if medium else None
            queue = link['queue']
            rows = [row for row in inventory.tcp_rows if row.get('bound_interface_index') == index]
            paths = dict(egress=direction(queue, {}, link['counters'], 'tx_bytes', nominal=nominal, device=name),
                         ingress=direction(None, {}, link['counters'], 'rx_bytes', nominal=nominal, device=name))
            identity = (index, link['mac'], link['kind'], link['available'], routes)
            if link['available']:
                monitor.observe(inventory.sampled, paths, rows, identity=identity,
                                socket_coverage=not inventory.tcp_truncated and not inventory.tcp_error)
            else:
                monitor.reset('interface unavailable')
        self.host = current

    def egress_admitted(self):
        return bool(self.service.lab and self.lab.view(max_age=self.max_age())['action'] == 'REVIEW_EGRESS_ALLOCATION')

    def query(self, request):
        keys(request, ('op', 'interface') if 'interface' in request else ('op',))
        interface = request.get('interface')
        if interface is not None and (not isinstance(interface, str) or not 1 <= len(interface) <= 15):
            raise ValueError('expected a valid interface name')
        lab = self.service.lab
        if lab and interface in (None, 'fc0'):
            row = self.lab.view(max_age=self.max_age())
            info = dict(row, scope='owned_lab_sender', interface='fc0',
                        socket_attribution='single owned veth, excluding sockets bound elsewhere')
        else:
            if interface is None:
                interface = next((name for name in self.host if name != 'lo'), next(iter(self.host), None))
            if interface is not None and interface not in self.host:
                raise ValueError('interface is outside the observed inventory')
            row = self.host[interface].view() if interface else Bottleneck().view()
            info = dict(row, scope='host_read_only', interface=interface,
                        socket_attribution='explicit INET_DIAG bound interface only')
        op = request['op']
        if op == 'capacity_estimate':
            return {key: info[key] for key in ('schema', 'state', 'scope', 'interface', 'sample_age_seconds',
                                              'generation', 'directions', 'capacity_reductions', 'last_capacity_reduction') if key in info}
        if op == 'qos_direction':
            owned = info['scope'] == 'owned_lab_sender'
            ingress = getattr(lab, 'ingress', None) if owned else None
            return dict(info, ingress_policy=dict(mode='ifb', capacity_kbit=ingress.capacity,
                                                  critical_tcp_source_port=9000, default_class='background')
                        if ingress else dict(mode='disabled'),
                        egress_policy=self.service.active if owned else None)
        if op == 'path_diagnostics':
            if info['scope'] == 'owned_lab_sender':
                state = lab.network(0)
                events = [row for row in self.service.events if row.get('source') == 'sender']
            else:
                state = self.service.inventory.view
                events = [row for row in self.service.inventory.events if row.get('interface') in (None, interface)]
            links = [row for row in state.get('interfaces', []) if row['name'] == info['interface']]
            indices = {row['index'] for row in links}
            return dict(info, adaptation=advisory(info), links=links, routes=[row for row in state.get('routes', [])
                                                 if row.get('interface_index') in indices][:32],
                        events=events[-16:])
        return info
