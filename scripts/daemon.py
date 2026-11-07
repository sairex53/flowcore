"""Single-owner Unix socket service for FLOWCORE's isolated laboratory."""
import argparse
import fcntl
import json
import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

from benchmark import ROOT
from network_state import attrs, messages, snapshot, subscribe
from observability import Observer
from ipc_security import authorize, decode, notify, private_directory, private_lock
from platform_network import Inventory
from platform_policy import PolicyStore, merged
from adaptive import Controller
from bottleneck_diagnostics import Diagnostics
from directional_qos import Ingress
from service_config import keys, load, profile
from service_lab import ServiceLab
from intent_session import Sessions

LIMIT = 65536
from paths import RUNTIME, SOCKET, STATE, VERSION


class Service:
    def __init__(self, config):
        self.config = config
        self.operations = None
        self.lab = None
        self.active = 'default'
        self.profile = config['lab']
        self.started = time.monotonic()
        self.events = deque(maxlen=64)
        self.watches = []
        self.telemetry = {}
        self.telemetry_timing = {}
        self.error = None
        self.sampled = 0
        self.capabilities = {}
        self.observer = Observer(config['observability'])
        self.diagnostics = Diagnostics(self)
        self.controller = Controller(self, egress_gate=self.diagnostics.egress_admitted)
        self.container_attachment = None
        self.container_attachments = {}
        self.monitor_only = False
        from workloads import Workloads
        self.workloads = Workloads()
        self.platform_policies = PolicyStore()
        self.inventory = Inventory(self.platform_policies, config['observability']['max_sockets'])
        from intent_v2 import IntentAPI
        self.intent_api = IntentAPI(self)
        self.research = False
        self.research_link_up = True
        self.research_mode = None
        self.research_step_done = False

    def log(self, event, **fields):
        if self.config['log_level'] == 'info' or event == 'error':
            print(json.dumps(dict(time=time.time(), event=event, **fields)), flush=True)

    def close_lab(self):
        self.diagnostics.lab.reset('lab closed or replaced')
        self.container_attachment = None
        self.container_attachments = {}
        self.controller.detach()
        self.observer.close()
        for watcher in self.watches:
            watcher.close()
        self.watches.clear()
        if self.lab:
            lab = self.lab
            self.lab = None
            lab.close()
            lab.storage.cleanup()
        self.telemetry = {}
        self.error = None
        self.sampled = 0

    def replace(self, name, settings, ingress=None):
        if name not in self.config['policies']:
            raise ValueError('unknown policy')
        profile(settings)
        if (self.lab and name == self.active and settings == self.profile
                and all(holder.poll() is None for holder in self.lab.holders)):
            return False
        storage = tempfile.TemporaryDirectory(prefix='lab-', dir=RUNTIME)
        candidate = ServiceLab(Path(storage.name))
        candidate.storage = storage
        watches = []
        try:
            candidate.start()
            policy = self.config['policies'][name]
            candidate.install_policy(policy, settings)
            selected_ingress = ingress if ingress is not None else getattr(self.lab, 'ingress', None)
            if selected_ingress is not None:
                selected_ingress.install(candidate)
                candidate.ingress = selected_ingress
            # Admission requires usable telemetry before replacing a working policy.
            candidate.counters()
            self.capabilities[policy['scheduler']] = 'verified'
            for index in (0, 1):
                watches.append(candidate.watcher(index))
        except BaseException:
            for watcher in watches:
                watcher.close()
            candidate.close()
            storage.cleanup()
            raise
        self.close_lab()
        self.lab, self.watches = candidate, watches
        self.active, self.profile = name, settings
        self.research_link_up = True
        self.research_mode = None
        self.research_step_done = False
        self.observer.start(candidate)
        self.log('lab_ready', policy=name, profile=settings)
        return True

    def poll_metrics(self):
        if not self.lab:
            return
        self.sampled = time.monotonic()
        try:
            if any(holder.poll() is not None for holder in self.lab.holders):
                self.close_lab()
                raise RuntimeError('namespace holder exited; lab closed')
            begin = time.monotonic_ns()
            self.telemetry = self.lab.counters()
            collected = time.monotonic_ns()
            self.observer.sample(self.lab, self.telemetry, self.profile['bandwidth_kbit'])
            observed = time.monotonic_ns()
            self.diagnostics.lab_sample(self.telemetry, collected/1e9)
            self.controller.sample_prediction()
            self.telemetry_timing = dict(time_ns=begin, counters_ns=collected-begin,
                                         observer_ns=observed-collected,
                                         prediction_ns=time.monotonic_ns()-observed)
            if any(row['active'] for row in self.container_attachments.values()):
                from containers import attachment_sample
                attachment_sample(self)
            self.error = None
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            self.telemetry = {}
            self.diagnostics.lab.reset('lab telemetry unavailable')
            self.error = str(exc)
            self.log('error', detail=self.error)

    def status(self):
        return dict(healthy=self.error is None and self.inventory.error is None,
                    configuration_generation=self.operations.checkpoint['generation'] if self.operations else 0,
                    reload_errors=self.operations.reload_errors if self.operations else 0,
                    previous_unclean=self.operations.previous_unclean if self.operations else False,
                    host_mutations_enabled=False,
                    monitor_only=self.monitor_only, inventory_error=self.inventory.error, uptime_seconds=round(time.monotonic()-self.started, 2),
                    lab_up=self.lab is not None, policy=self.active, profile=self.profile,
                    controlled_interfaces=[self.config['interface']] if self.lab else [],
                    capabilities=self.capabilities, error=self.error)

    def command(self, request, uid=None, pid=None, passed_fd=None):
        if not isinstance(request, dict):
            raise ValueError('expected JSON object')
        op = request.get('op')
        if not isinstance(op,str):
            raise ValueError('expected operation name')
        authorize(op,self.monitor_only,os.geteuid() if uid is None else uid,os.geteuid())
        if op == 'intent_v2_revoke':
            keys(request,('op','name'))
            return self.intent_api.revoke(request.get('name'),uid,pid)
        if op == 'intent_v2':
            return self.intent_api.call(request,uid,pid,passed_fd)
        if op.startswith('ops_'):
            keys(request,('op',))
            if self.operations is None:raise RuntimeError('deployment operations not initialized')
            if op=='ops_status':return self.operations.status()
            if op=='ops_validate':return self.operations.reload(dry_run=True)
            if op=='ops_reload':return self.operations.reload()
            if op=='ops_emergency':
                self.close_lab()
                return dict(stopped=True,host_network_modified=False)
            if op=='ops_diagnostics':
                return dict(version=1,state=self.status(),operations=self.operations.status(),
                            inventory=self.inventory.status(),bpf=self.observer.bpf.status())
            raise ValueError('unsupported operational command')
        if op == 'workload_list':
            return self.workloads.view(uid)
        if op == 'workload_register':
            keys(request,('op','pid','service','container','unit'))
            return self.workloads.register(uid,**{key:request[key] for key in ('pid','service','container','unit')})
        if op == 'workload_forget':
            keys(request,('op','id'))
            return self.workloads.forget(uid,request['id'])
        if op == 'platform_workload_attach':
            keys(request,('op','id'))
            if os.geteuid()!=0:
                raise PermissionError('workload classification requires privileged owned laboratory')
            self.workloads.refresh()
            row=self.workloads.rows.get(request['id'])
            if not row or row['owner_uid']!=uid or row['state']!='active':
                raise ValueError('workload is not an active owned registration')
            from containers import attach, cgroup
            if cgroup(row['pid'])!=row['identity']:
                raise ValueError('workload identity changed')
            resolved=merged(self.platform_policies.state['policy'],'veth','fc0',row['service'],
                            row['metadata'].get('container_name'),row['identity']['path'])
            if any(resolved['fields'][field] for field in ('min_kbit','max_kbit','desired_kbit')):
                raise ValueError('individual bandwidth contracts require dedicated classes; only class assignment supported')
            result=attach(self,row['pid'],resolved['fields']['priority'])
            result['workload_id']=row['id']
            result['resolved_policy']=resolved
            return result
        if op == 'platform_workload_classifiers':
            return list(self.container_attachments.values())
        if op in ('bottleneck_inspect', 'capacity_estimate', 'qos_direction', 'path_diagnostics'):
            return self.diagnostics.query(request)
        if op == 'platform_interfaces':
            return self.inventory.status()
        if op == 'platform_policies':
            return self.platform_policies.state
        if op == 'platform_resolve':
            return merged(self.platform_policies.state['policy'],
                          *(request.get(key) for key in ('device','interface','service','container','cgroup')))
        if op == 'platform_container_status':
            return self.container_attachment
        if op == 'platform_container_attach':
            if os.geteuid()!=0:
                raise PermissionError('classifier requires privileged lab')
            from containers import attach
            return attach(self,request.get('pid'),request.get('traffic_class'))
        if op == 'platform_commit':
            return self.platform_policies.commit(request.get('policy'),request.get('revision'),uid)
        if op == 'platform_apply_intent':
            if os.geteuid()!=0:
                raise PermissionError('profile application requires the privileged laboratory')
            if not self.lab or self.config['policies'][self.active]['scheduler']!='htb':
                raise ValueError('profile application requires the owned HTB laboratory')
            name = request.get('service')
            resolved = merged(self.platform_policies.state['policy'],'veth','fc0',name,request.get('container'))
            result = self.controller.register(dict(resolved['fields'],name=name),uid,pid)
            return dict(registration=result,provenance=resolved['provenance'])
        if op in ('lab_up','lab_configure','policy_apply','controller_start','controller_shadow',
                  'research_conditions','platform_apply_intent') and os.geteuid()!=0:
            raise PermissionError('privileged operation requires the laboratory daemon')
        if op in ('research_conditions', 'research_sample', 'research_snapshot', 'research_reservation_step'):
            if not self.research:
                raise ValueError('research commands require a research daemon')
            if op == 'research_conditions':
                from research_faults import apply_conditions
                return apply_conditions(self, request.get('network'), request.get('mode'), request.get('seed'), request.get('link_up'))
            if op == 'research_reservation_step':
                if not self.lab or self.research_mode != 'static-step' or self.research_step_done or self.controller.mode != 'off':
                    raise ValueError('requires an unused static-step research lab')
                from research_faults import reservation_step
                timing = reservation_step(self)
                self.research_step_done = True
                return dict(applied_ns=timing['verified_ns'], timing=timing)
            if op == 'research_snapshot':
                if not self.lab:
                    raise ValueError('research snapshot requires a lab')
                lab = self.lab
                begin = time.monotonic_ns()
                counters = lab.counters()
                links = [lab.network(i)['interfaces'] for i in (0, 1)]
                end = time.monotonic_ns()
                result = dict(begin_ns=begin, end_ns=end, tc=counters, links=links,
                              namespaces=[os.fstat(fd).st_ino for fd in lab.namespace_fds])
                if request.get('setup') is True:
                    result['setup'] = {name: (lab.output/name).read_text() for name in
                                       ('offloads-fc0.txt', 'offloads-fc1.txt', 'tcp.txt')}
                return result
            view = self.observer.snapshot()
            return dict(time_ns=view['time_ns'], tc=self.telemetry, overhead=view['overhead'],
                        telemetry_timing=self.telemetry_timing,
                        backend=view['bpf'], state=self.controller.status(),
                        sample_ns=self.observer.sampled_ns,
                        control_window=self.observer.window(view['time_ns'],
                            self.controller.cfg['window_seconds'])['application'],
                        buckets={str(tick): dict(bucket.counters)
                                 for tick,bucket in self.observer.windows.buckets.items()
                                 if tick >= view['time_ns']//1000000000-5})
        controller = self.controller
        registry = controller.registry
        if op == 'intent_register':
            return controller.register(request.get('intent'), uid, pid)
        if op in ('intent_list', 'intent_inspect', 'intent_remove'):
            self.intent_api.expire()
            registry.expire()
            if op == 'intent_list':
                return {name: registry.inspect(name) for name in registry.rows}
            if op == 'intent_inspect':
                return registry.inspect(request.get('name'))
            registry.remove(request.get('name'), uid, request.get('instance_token'))
            controller.safe_tick()
            return {'removed': True}
        if op == 'controller_status':
            return controller.status()
        if op in ('controller_start', 'controller_shadow'):
            controller.start('shadow' if op == 'controller_shadow' else 'active', request.get('strategy', 'slo'))
            return controller.status()
        if op == 'controller_observe':
            controller.start('observe', request.get('strategy','hybrid'))
            return controller.status()
        if op == 'controller_fallback':
            controller.stop('operator emergency fallback')
            controller.state.move('SAFE_FALLBACK','operator emergency fallback')
            controller.error = 'operator emergency fallback'
            controller.record(result='safe_fallback', reason=controller.error)
            return controller.status()
        if op == 'controller_stop':
            controller.stop('operator stop')
            return controller.status()
        if op == 'policy_evaluate':
            return controller.evaluate()
        if op in ('policy_explain', 'policy_history'):
            return (controller.history[-1] if controller.history else None) if op == 'policy_explain' else list(controller.history)
        if op in ('request_start', 'request_complete', 'request_sample'):
            registry.expire()
            name = request.get('service')
            row = registry.rows.get(name) if isinstance(name, str) else None
            token = request.get('instance_token')
            if row and (row['owner_uid'] != uid or
                        (token is not None and (not isinstance(token, str) or token not in row['instances']))):
                raise ValueError('invalid intent owner or instance token')
            return self.observer.ingest(request, uid)
        if op == 'lifecycles':
            limit, offset = request.get('limit',128), request.get('offset',0)
            if type(limit) is not int or not 1<=limit<=128 or type(offset) is not int or not 0<=offset<=2048:
                raise ValueError('lifecycle pagination exceeds bounds')
            rows = list(self.observer.lifecycles.rows.values())
            return dict(connections=rows[offset:offset+limit],total=len(rows),
                        generation=self.observer.generation,retention=self.observer.lifecycles.status(),
                        backend=self.observer.bpf.status(), completeness='observed events only; inspect backend loss counters')
        if op == 'connections':
            limit, offset = request.get('limit', 128), request.get('offset', 0)
            if type(limit) is not int or not 1 <= limit <= 128 or type(offset) is not int or not 0 <= offset <= 1024:
                raise ValueError('connections requires limit 1..128 and offset 0..1024')
            return dict(connections=self.observer.last_rows[offset:offset+limit], offset=offset,
                        total=len(self.observer.last_rows), truncated=self.observer.truncated,
                        sampled_ns=self.observer.sampled_ns, generation=self.observer.generation)
        if op == 'events':
            after = request.get('after', 0)
            if type(after) is not int or after < 0:
                raise ValueError('after must be a nonnegative sequence number')
            history = self.observer.events
            first = history[0]['sequence'] if history else 1
            last = self.observer.event_sequence
            if after > last:
                after = 0
            return dict(events=[event for event in history if event['sequence'] > after],
                        first_sequence=first, last_sequence=last,
                        retention_evictions=first - 1, missed=max(0, first - 1 - after))
        if op in ('observe', 'congestion', 'health'):
            interface = request.get('interface')
            view = self.observer.snapshot()
            if interface is not None:
                if self.lab:
                    if interface != self.config['interface']:
                        raise ValueError('interface is not the controlled sender interface')
                else:
                    found = [row for row in view['host_interfaces'] if row['name'] == interface]
                    if not found:
                        raise ValueError('unknown observed interface')
                    view['host_interfaces'] = found
                    view['interface_filter'] = interface
                    view['window_scope'] = 'all observed local sockets; unbound sockets are not attributed to this interface'
            if op == 'congestion':
                return {key: view[key] for key in ('time_ns', 'generation', 'windows', 'findings',
                                                   'sender_shaper_saturation_indicator')}
            if op == 'health':
                return dict(service=self.status(), backend=view['bpf'], collector_errors=view['collector_errors'],
                            sample_age_seconds=view['sample_age_seconds'], overhead=view['overhead'])
            return view
        if op in ('status', 'lab_status'):
            return self.status()
        if op == 'policies':
            return self.config['policies']
        if op == 'metrics':
            return dict(state=self.status(), tc=self.telemetry, events=list(self.events),
                        observability=self.observer.snapshot(), platform=self.inventory.status(),
                        controller=self.controller.status(),
                        bottleneck=self.diagnostics.query(dict(op="bottleneck_inspect")), policy_revision=self.platform_policies.state['revision'],
                        sample_age_seconds=round(time.monotonic()-self.sampled, 2) if self.sampled else None)
        if op == 'interfaces':
            return dict(host=snapshot(), lab=[self.lab.network(i) for i in (0, 1)] if self.lab else [])
        if op == 'inspect':
            return dict(state=self.status(), policy=self.config['policies'][self.active],
                        namespaces=[p.pid for p in self.lab.holders] if self.lab else [],
                        tc=self.lab.counters() if self.lab else {})
        if op == 'lab_down':
            self.close_lab()
            self.log('lab_down')
            return self.status()
        if op in ('lab_up', 'lab_configure', 'policy_apply'):
            name = request.get('name', self.active)
            if not isinstance(name, str):
                raise ValueError('policy name must be a string')
            settings = self.profile
            if op == 'lab_configure':
                settings = profile(request.get('profile'))
            if op == 'policy_apply' and not self.lab:
                raise ValueError('start the lab before applying a policy')
            ingress = None
            if 'ingress_kbit' in request:
                if op != 'lab_up' or self.lab:
                    raise ValueError('ingress can only be selected when starting a new lab')
                if self.research:
                    raise ValueError('legacy research fixtures require an unmodified directional topology')
                ingress = Ingress(request['ingress_kbit'])
            changed = self.replace(name, settings, ingress)
            self.poll_metrics()
            return dict(changed=changed, **self.status())
        raise ValueError('unknown operation')

    def event(self, sock, origin):
        try:
            data, _, flags, sender = sock.recvmsg(65536)
            if sender[0] or flags & socket.MSG_TRUNC:
                return
            for kind, _, _, body in messages(data):
                if kind in (16, 17, 20, 21, 24, 25):
                    if origin in ('sender', 'receiver') and self.lab:
                        self.lab.topology_dirty = True
                        self.diagnostics.lab.reset('lab link, address or route changed')
                    elif origin == 'host':
                        for monitor in self.diagnostics.host.values():
                            monitor.reset('host link, address or route changed')
                    row = dict(source=origin, type=kind, time=time.time())
                    if kind in (16, 17) and len(body) >= 16:
                        _, _, _, index, flags, _ = struct.unpack_from('=BBHiII', body)
                        name = attrs(body[16:]).get(3, b'').rstrip(b'\0').decode(errors='replace')
                        row.update(index=index, name=name, up=bool(flags & 1), carrier=bool(flags & 0x10000))
                    self.events.append(row)
        except OSError as error:
            self.events.append(dict(source=origin, error=str(error), resync='next interface snapshot'))


def receive(connection, descriptors=None, expected_peer=None):
    data = bytearray()
    deadline = time.monotonic() + 1
    while b'\n' not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('request deadline exceeded')
        connection.settimeout(remaining)
        if descriptors is None:
            block = connection.recv(min(4096, LIMIT + 1 - len(data)))
        else:
            block, ancillary, flags, _ = connection.recvmsg(min(4096,LIMIT+1-len(data)),
                                                          socket.CMSG_SPACE(12)+socket.CMSG_SPACE(32),socket.MSG_CMSG_CLOEXEC)
            supplied_peer=None
            for level,kind,payload in ancillary:
                if level==socket.SOL_SOCKET and kind==socket.SCM_CREDENTIALS and len(payload)==12:
                    supplied_peer=struct.unpack('3i',payload)
                if level==socket.SOL_SOCKET and kind==socket.SCM_RIGHTS:
                    descriptors.extend(x[0] for x in struct.iter_unpack('=i',payload[:len(payload)//4*4]))
            if flags & socket.MSG_CTRUNC or len(descriptors)>1:
                raise ValueError('descriptor limit exceeded')
            if expected_peer is not None and supplied_peer != expected_peer:
                raise PermissionError('connection sender differs from authenticated peer')
        if not block:
            raise ValueError('missing request terminator')
        data.extend(block)
        if len(data) > LIMIT:
            raise ValueError('request too large')
    return decode(data)


def handle(server, service):
    conn, _ = server.accept()
    with conn:
        peer = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        pid, uid, _ = peer
        if uid not in (0,os.geteuid()):
            return
        descriptors=[]
        try:
            request=receive(conn,descriptors,peer)
            if descriptors and not (request.get('op')=='intent_v2' and request.get('action')=='associate'):
                raise ValueError('descriptors forbidden for this operation')
            result = dict(ok=True, result=service.command(request, uid, pid, descriptors[0] if descriptors else None))
        except (OSError, ValueError, RuntimeError, RecursionError, TypeError, KeyError, OverflowError, struct.error, subprocess.SubprocessError) as error:
            detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
            service.log('error', detail=detail)
            category = ('forbidden' if isinstance(error,PermissionError) else
                        'invalid_request' if isinstance(error,(ValueError,RecursionError)) else 'operation_failed')
            result = dict(ok=False,error=detail,code=category)
        finally:
            for fd in descriptors:
                os.close(fd)
        try:
            response = json.dumps(result).encode() + b'\n'
            if len(response) > 1024 * 1024:
                response = b'{"ok":false,"error":"response too large"}\n'
            conn.settimeout(1)
            conn.sendall(response)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(prog='flowcored', description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config/service.json')
    parser.add_argument('--research', action='store_true', help='enable owned lab fault controls')
    parser.add_argument('--monitor-only',action='store_true',help='disable all mutations')
    parser.add_argument('--check-config',action='store_true',help='validate configuration without opening sockets')
    parser.add_argument('--version',action='version',version=VERSION)
    args = parser.parse_args()
    try:
        config=load(args.config)
        if args.check_config:
            print(json.dumps(dict(valid=True,host_mutations_enabled=False)))
            return 0
        service = Service(config)
        if args.monitor_only and args.research:
            raise ValueError('research and monitor-only modes are exclusive')
        service.research = args.research
        service.monitor_only = args.monitor_only
        os.umask(0o077)
        private_directory(RUNTIME)
        if STATE != RUNTIME:
            private_directory(STATE)
        with private_lock(RUNTIME/'daemon.lock') as lock, private_lock(STATE/'state.lock') as state_lock:
            fcntl.flock(state_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for stale in RUNTIME.glob('lab-*'):
                if stale.is_dir() and not stale.is_symlink():
                    shutil.rmtree(stale)
            from operations import Operations
            service.operations=Operations(service,STATE/'recovery.json',args.config)
            service.operations.start()
            service.controller.restore(STATE)
            service.platform_policies.restore(STATE)
            service.inventory.refresh()
            service.diagnostics.host_sample()
            service.workloads.refresh()
            service.observer.sample_host(service.inventory)
            if SOCKET.exists() or SOCKET.is_symlink():
                import stat
                info=SOCKET.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.geteuid():
                    raise PermissionError('refusing non-owned socket path')
                SOCKET.unlink()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server, subscribe() as host, Sessions(service,RUNTIME/'intent.sock') as sessions:
                server.setsockopt(socket.SOL_SOCKET,socket.SO_PASSCRED,1)
                server.bind(str(SOCKET))
                socket_inode=SOCKET.lstat().st_ino
                try:
                    server.listen(16)
                    stopped = False
                    def stop(*_):
                        nonlocal stopped
                        stopped = True
                    signal.signal(signal.SIGTERM, stop)
                    signal.signal(signal.SIGINT, stop)
                    reload_pending=False
                    def reload_signal(*_):
                        nonlocal reload_pending
                        reload_pending=True
                    signal.signal(signal.SIGHUP,reload_signal)
                    service.log('ready', socket=str(SOCKET), lab_privileged=os.geteuid() == 0)
                    notify('READY=1')
                    inventory_tick = time.monotonic()
                    while not stopped:
                        if reload_pending:
                            reload_pending=False
                            try:service.operations.reload()
                            except (OSError,ValueError,RuntimeError) as error:service.log('error',detail='reload rejected: '+str(error))
                        watchers = [host, *service.watches]
                        observer_fd = service.observer.bpf.fd()
                        inputs = [server, *watchers, *sessions.inputs()] + ([observer_fd] if observer_fd is not None else [])
                        ready, _, _ = select.select(inputs, [], [], .25 if service.lab else 1)
                        if observer_fd is not None:
                            service.observer.drain()
                        for index, watcher in enumerate(watchers):
                            if watcher in ready:
                                service.event(watcher, ('host', 'sender', 'receiver')[index])
                        if server in ready:
                            handle(server, service)
                        sessions.poll(ready)
                        if service.lab and time.monotonic()-service.sampled >= service.config['telemetry_seconds']:
                            service.poll_metrics()
                        service.intent_api.expire()
                        service.controller.safe_tick()
                        if time.monotonic()-inventory_tick>=2:
                            service.inventory.refresh()
                            service.diagnostics.host_sample()
                            service.workloads.refresh()
                            if not service.lab:
                                service.observer.sample_host(service.inventory)
                            inventory_tick = time.monotonic()
                            notify('WATCHDOG=1')
                finally:
                    notify('STOPPING=1')
                    service.close_lab()
                    if SOCKET.exists() and SOCKET.lstat().st_ino==socket_inode:
                        SOCKET.unlink()
                    service.operations.finish()
                    service.log('stopped')
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps(dict(event='fatal', detail=str(error))), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
