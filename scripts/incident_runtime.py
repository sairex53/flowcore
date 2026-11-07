"""Translate existing daemon telemetry to the causal incident schema."""
import hashlib
import json
import os
import secrets
import resource
import stat
import time

from causal_diagnostics import MAX_BUNDLE, NAME, NS, TOKEN, WINDOW, Recorder, decode_bundle, metrics, replay
from service_config import keys


def queue_records(recorder, stamp, interface, view):
    for direction, row in view.get('directions', {}).items():
        recorder.record('queue', stamp, interface=interface, direction=direction,
                        backlog=row['backlog_bytes'], drops=row['dropped_packets'], throughput=row['throughput_kbit'],
                        capacity=row['capacity_kbit'], saturated=row['saturated'], owned=row['controllable'])
        for name, cls in row['classes'].items():
            recorder.record('queue', stamp, interface=interface, direction=direction, traffic_class=name,
                            backlog=cls['backlog_bytes'], drops=cls['dropped_packets'], throughput=cls['served_kbit'],
                            demand=cls['queue_accounted_demand_kbit'])
    if (tcp := view.get('transport')) is not None:
        recorder.record('tcp', stamp, interface=interface, rtt_us=tcp['rtt_us'], min_rtt_us=tcp['min_rtt_us'],
                        rttvar_us=tcp['rttvar_us'], retransmits=tcp['retransmit_segments'],
                        segments=tcp['data_segments'], receiver_limited=tcp['receiver_pressure'],
                        path_pressure=tcp['path_pressure'], coverage=bool(tcp['matched_sockets']))


def app_values(row, target):
    return dict(traffic_class=row['traffic_class'], successes=row['application']['ok'],
                failures=row['application']['timeout']+row['application']['failure'],
                attempts=row['application']['ok']+row['application']['timeout']+row['application']['failure'],
                timeouts=row['application']['timeout'], deadlines=row['application']['deadline_reported'],
                deadline_misses=row['application']['deadline_miss'], p99_us=row['application']['p99_us'],
                target_us=target*1000)


class IncidentRuntime:
    def __init__(self, service):
        self.service = service
        self.recorder = Recorder()
        self.salt = secrets.token_bytes(16)
        self.last_network_sample = None
        self.cpu = time.process_time_ns()
        self.stamp = time.monotonic_ns()
        self.scope_key = None
        self.last_window = self.stamp//WINDOW*WINDOW
        self.request_limits = {}
        self.policy_generation = 0

    def reset(self):
        self.recorder.reset_scope()
        self.last_window = time.monotonic_ns()//WINDOW*WINDOW
        self.request_limits.clear()
        self.scope_key = None
        self.last_network_sample = None

    def policy(self, policy, capacity):
        self.policy_generation += 1
        self.recorder.record('policy', time.monotonic_ns(), policy=policy, capacity=capacity,
                             generation=self.policy_generation)

    def decision(self, fields):
        result = fields['result']
        name = fields.get('service')
        self.recorder.record('controller', time.monotonic_ns(), name if isinstance(name,str) and NAME.fullmatch(name) else None,
                             result=result, generation=self.policy_generation, comparable=fields.get('comparable'),
                             degraded=fields.get('degraded'), changed=False,
                             post_start_ns=fields.get('post_start_ns'), post_end_ns=fields.get('post_end_ns'))

    def sample(self, stamp, interface, view, *, scope, healthy, sample_id=None):
        if scope != self.scope_key:
            if self.scope_key is not None:
                self.reset()
            self.scope_key = scope
        if interface and TOKEN.fullmatch(interface) and (sample_id is None or sample_id != self.last_network_sample):
            queue_records(self.recorder, stamp, interface, view)
            self.last_network_sample = sample_id
        if not healthy:
            self.recorder.record('quality', stamp, reason='network_collection_partial')
        cpu = time.process_time_ns()
        if stamp > self.stamp:
            self.recorder.record('resource', stamp, cpu_pct=(cpu-self.cpu)*100/(stamp-self.stamp),
                                 memory_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
                                 source='daemon_cpu_and_peak_rss')
            self.cpu, self.stamp = cpu, stamp

    def request(self, *, service, traffic_class, stamp, start, outcome, duration, deadline, identity=None, cookie=None,
                socket_identity=None, timings=None, protocol=None):
        # Bounded per-service detail; the separate observer histogram still counts
        # every accepted report, including failures and deadline-bearing requests.
        tick = stamp//(NS//8)
        if service not in self.request_limits and len(self.request_limits) >= 64:
            return
        if tick <= self.request_limits.get(service,-1):
            return
        self.request_limits[service] = tick
        def digest(value):
            return hashlib.blake2s(value.encode(), key=self.salt, digest_size=8).hexdigest()
        detail = dict(traffic_class=traffic_class, start_ns=start, outcome=outcome,
                      duration_us=duration, deadline_us=deadline,
                      association='fd_only' if cookie is not None else 'unavailable',
                      request_ref=digest(identity) if identity else None,
                      socket_ref=digest(str(cookie)) if cookie is not None else None, transport=protocol)
        observer = self.service.observer
        if (socket_identity and cookie is not None and observer.sampled_ns is not None
                and start <= observer.sampled_ns <= stamp and not observer.truncated and not observer.errors):
            hex_cookie = f'{cookie:016x}'
            found = []
            for ns in ('sender','receiver','host'):
                row = observer.current.get((ns,hex_cookie))
                if row and row['state'] == 'established' and socket_identity == tuple(row[key] for key in
                        ('family','local_address','local_port','remote_address','remote_port')):
                    found.append(row)
            if len(found) == 1:
                row = found[0]
                index = row['bound_interface_index']
                links = observer.host_interfaces if row['namespace'] == 'host' else []
                interface = next((link['name'] for link in links if link.get('index') == index), None)
                lab = self.service.lab
                association = 'kernel_bound' if interface else None
                port_class = None
                if lab and not lab.topology_dirty and row['namespace'] == 'sender':
                    if index == lab.sender_index:
                        interface, association = 'fc0', 'kernel_bound'
                    elif index is None and row['local_address'] in lab.sender_addresses:
                        interface, association = 'fc0', 'owned_veth_address_match'
                    if interface == 'fc0':
                        classes = self.service.config['policies'][self.service.active].get('classes',{})
                        port_class = next((name for name,policy in classes.items()
                                           if policy['port']==row['remote_port']),None)
                tcp = row['tcp_info']
                detail.update(association='fd_and_sample', cgroup_id=row['cgroup_id'], netns_id=row['netns'],
                              interface=interface if interface and TOKEN.fullmatch(interface) else None,
                              interface_association=association, observed_class=port_class, sample_ns=observer.sampled_ns, rtt_us=tcp.get('rtt_us'), min_rtt_us=tcp.get('min_rtt_us'))
            elif len(found) > 1:
                detail['association'] = 'ambiguous'
        self.recorder.record('request', stamp, service, **detail, **(timings or {}))

    def tick(self, now=None):
        now = time.monotonic_ns() if now is None else now
        # Accept the existing two-second ingress lag before finalizing buckets.
        end = (now-2*NS)//WINDOW*WINDOW
        if end <= self.last_window:
            return
        self.last_window = end
        observer = self.service.observer
        # Observer aligns completed buckets by second. Summarize the exact four
        # seconds preceding the nominal window end, independently of tick jitter.
        view = observer.window(end, WINDOW//NS, complete=True)
        rows = [row for row in self.recorder.events if row['scope'] == self.recorder.scope and end-WINDOW <= row['time_ns'] < end]
        roots = [metrics(row) for row in rows if row['kind']=='queue' and metrics(row)['direction']=='egress'
                 and metrics(row).get('traffic_class') is None]
        tcps = [metrics(row) for row in rows if row['kind']=='tcp' and metrics(row)['coverage']]
        queue_available = len(roots) >= 2
        rates = [row['throughput'] for row in roots if row.get('throughput') is not None]
        quality = 'partial' if not queue_available or any(row['kind']=='quality' for row in rows) else 'sampled'

        def total(group, key):
            return sum(row[key] for row in group) if len(group) >= 2 and all(row.get(key) is not None for row in group) else None

        self.recorder.window(now, end, throughput=sum(rates)/len(rates) if len(rates)>=2 else None,
                             drops=total(roots,'drops'), retransmits=total(tcps,'retransmits'),
                             segments=total(tcps,'segments'), quality=quality)
        for name, app in view['application'].items():
            if app['application']['samples'] or app['application']['timeout'] or app['application']['failure'] or name in self.recorder.latest:
                cfg = observer.cfg['services'][name]
                values = app_values(app, cfg['slo_ms'])
                classes = [metrics(row) for row in rows if row['kind']=='queue' and metrics(row)['direction']=='egress'
                           and metrics(row).get('traffic_class')==values['traffic_class']]
                class_rates = [row['throughput'] for row in classes if row.get('throughput') is not None]
                self.recorder.window(now, end, name, **values, quality=quality,
                                     throughput=sum(class_rates)/len(class_rates) if len(class_rates)>=2 else None)
        valid_services = observer.cfg['services'].keys() | {None}
        for name in self.recorder.latest.keys()-valid_services:
            self.recorder.latest.pop(name)
            self.recorder.prior.pop(name, None)
        for name in self.request_limits.keys()-valid_services:
            self.request_limits.pop(name)

    def command(self, request):
        op = request['op']
        if op == 'diagnose':
            keys(request, ('op','service') if 'service' in request else ('op',))
            service = request.get('service')
            if service is not None and (not isinstance(service,str) or service not in self.service.observer.cfg['services']):
                raise ValueError('unknown diagnostic service')
            if service is not None:
                return self.recorder.diagnose(service)
            return dict(schema=1, network=self.recorder.diagnose(None),
                        services=[self.recorder.diagnose(name) for name in sorted(self.service.observer.cfg['services'])])
        if op == 'incident_list':
            keys(request, ('op',))
            return dict(schema=1, retention=16, evicted=self.recorder.incident_evictions, incidents=[
                dict(id=bundle['id'], service=bundle['events'][-1]['service'],
                     time_ns=bundle['events'][-1]['time_ns'], event_count=len(bundle['events']))
                for bundle in reversed(self.recorder.incidents)])
        keys(request, ('op','id'))
        identifier = request['id']
        if not isinstance(identifier,str) or len(identifier)>64:
            raise ValueError('invalid incident ID')
        bundle = self.recorder.find(identifier)
        return bundle if op == 'incident_export' else replay(bundle)


def replay_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BUNDLE:
            raise ValueError('expected a bounded regular incident file')
        return replay(decode_bundle(stream.read(MAX_BUNDLE+1)))
