"""Unprivileged Fabric agent. Local daemon telemetry is optional."""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import time

from durable_state import atomic_json, read_json
from fabric_actuator import desired, local
from fabric_coordinator import MAX_GENERATION, MAX_RATE, names
from fabric_protocol import (Rejected, audit, endpoint, fields, hexdigest, ident, integer,
                             locked_state, remote, tls)
from network_state import snapshot


def config(value):
    fields(value, ('cluster', 'host', 'coordinator', 'tls', 'interfaces', 'services'),
           ('egress', 'daemon_socket', 'poll_ms', 'cooperative'))
    ident(value['cluster'])
    ident(value['host'])
    endpoint(value['coordinator'])
    names(value['interfaces'])
    names(value['services'])
    for interface in value['interfaces']:
        ident(interface, 15)
    integer(value.setdefault('poll_ms', 650), 250, 1500)
    if 'daemon_socket' in value:
        absolute(value['daemon_socket'])
    if 'cooperative' in value:
        row = fields(value['cooperative'], ('summary_path', 'service'), ('advice_path',))
        absolute(row['summary_path'])
        if 'advice_path' in row:
            absolute(row['advice_path'])
            if row['advice_path'] == row['summary_path'] or 'egress' not in value:
                raise ValueError('advice requires a distinct path and an egress actuator')
        if row['service'] not in value['services']:
            raise ValueError('cooperative service is outside the local inventory')
    if 'egress' in value:
        row = fields(value['egress'], ('resource', 'interface', 'floor_kbit', 'max_kbit', 'socket'))
        ident(row['resource'])
        if row['interface'] not in value['interfaces']:
            raise ValueError('egress interface missing from local inventory')
        floor = integer(row['floor_kbit'], 100, MAX_RATE)
        integer(row['max_kbit'], floor, MAX_RATE)
        absolute(row['socket'])
    return value


def absolute(path):
    if not isinstance(path, str) or len(path) > 107 or not Path(path).is_absolute():
        raise ValueError('expected absolute Unix socket path')


def daemon_metrics(path):
    deadline = time.monotonic() + .6
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(.6)
        sock.connect(path)
        sock.sendall(b'{"op":"metrics"}\n')
        data = bytearray()
        while not data.endswith(b'\n'):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('local metrics deadline')
            sock.settimeout(remaining)
            chunk = sock.recv(32768)
            if not chunk:
                raise RuntimeError('local daemon closed metrics connection')
            data.extend(chunk)
            if len(data) > 1024 * 1024:
                raise ValueError('local metrics exceed limit')
        reply = json.loads(data)
        if not reply['ok']:
            raise RuntimeError('local daemon metrics unavailable')
        return reply['result']


def maybe_ident(value):
    try:
        return ident(value)
    except ValueError:
        return None


class Collector:
    def __init__(self, cfg):
        self.cfg, self.previous = cfg, None

    def sample(self):
        now = time.monotonic_ns()
        data = dict(age_ms=0, interfaces=[], services=[], daemon_healthy=None,
                    controller=None, bottleneck=None, egress_kbit=None, decision=None)
        try:
            rows = snapshot(sysfs_speed=False)['interfaces']
        except (OSError, ValueError, RuntimeError):
            rows = []
        for row in rows:
            if row['name'] not in self.cfg['interfaces']:
                continue
            data['interfaces'].append({key: row[key] for key in ('name', 'mtu', 'up')})
            if row['name'] == self.cfg.get('egress', {}).get('interface'):
                count = row['counters'].get('tx_bytes')
                previous = self.previous
                if (previous and count is not None and previous[0] == row['index'] and
                        count >= previous[1] and 0 < now-previous[2] <= 3_000_000_000):
                    data['egress_kbit'] = min(MAX_RATE*100, (count-previous[1])*8_000_000//(now-previous[2]))
                self.previous = (row['index'], count, now) if count is not None else None
        if 'daemon_socket' in self.cfg:
            try:
                self.enrich(data, daemon_metrics(self.cfg['daemon_socket']))
            except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                data['daemon_healthy'] = False
        data['age_ms'] = min(120000, (time.monotonic_ns()-now)//1_000_000)
        return data

    def enrich(self, data, metrics):
        state = metrics['controller']
        data['daemon_healthy'] = metrics['state']['healthy'] is True
        data['controller'] = maybe_ident(state.get('engine_state'))
        data['controller_actions'] = state.get('actions')
        data['controller_session'] = maybe_ident(state.get('controller_session'))
        data['decision'] = maybe_ident((state.get('last_decision') or {}).get('action'))
        data['bottleneck'] = maybe_ident(metrics['bottleneck'].get('classification'))
        observation = metrics['observability']
        age = observation['sample_age_seconds']
        windows = observation['windows']
        if not windows or type(age) not in (int, float) or not 0 <= age <= 3:
            return
        data['sample_end_ns'] = observation['time_ns']
        shortest = min(windows.values(), key=lambda window: window['seconds'])
        for name in self.cfg['services']:
            row = shortest['application'].get(name, {}).get('application')
            if not row:
                continue
            p99 = row['p99_us']
            data['services'].append(dict(name=name, samples=row['samples'],
                errors=row['failure']+row['timeout'], misses=row['deadline_miss'],
                window_seconds=shortest['seconds'],
                p99_us=round(p99) if p99 is not None and math.isfinite(p99) else None))


class Agent:
    def __init__(self, cfg, path):
        self.cfg, self.path = config(cfg), path
        self.collector = Collector(cfg)
        self.context = tls(cfg['tls'])
        self.session, self.seq = None, 0
        self.fleet_metrics = False
        self.cooperative = False
        self.boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.local = cfg.get('egress')
        self.ack = None
        self.durability_failed = False
        self.capabilities = ['telemetry_v1', *(['cap_v1'] if self.local else []),
                             *(['fleet_metrics_v1'] if 'daemon_socket' in cfg else []),
                             *(['cooperative_slo_v1'] if 'cooperative' in cfg else [])]
        try:
            self.state = read_json(path)
            fields(self.state, ('cluster', 'host', 'epoch', 'generation', 'rate_kbit'))
            if any(self.state[key] != cfg[key] for key in ('cluster', 'host')):
                raise ValueError('agent checkpoint identity mismatch')
            hexdigest(self.state['epoch'], 32)
            integer(self.state['generation'], 1, MAX_GENERATION)
            integer(self.state['rate_kbit'], 100, MAX_RATE)
        except FileNotFoundError:
            self.state = None

    def call(self, operation, **fields):
        return remote(self.context, self.cfg['coordinator'], dict(op=operation,
                      host=self.cfg['host'], cluster=self.cfg['cluster'], **fields))

    def apply(self, policy):
        if self.durability_failed:
            raise Rejected('durability_error')
        value = desired(policy)
        if not self.local or any(value[key] != self.cfg[key] for key in ('cluster', 'host')) or any(
                value[key] != self.local[key] for key in ('resource', 'interface')) or not (
                self.local['floor_kbit'] <= value['rate_kbit'] <= self.local['max_kbit']):
            raise Rejected('local_scope')
        old = self.state
        new = {key: value[key] for key in ('cluster', 'host', 'epoch', 'generation', 'rate_kbit')}
        if old is not None:
            if old['epoch'] != new['epoch'] or old['generation'] > new['generation']:
                raise Rejected('stale_generation')
            if old['generation'] == new['generation'] and old != new:
                raise Rejected('generation_conflict')
        if old != new:
            try:
                atomic_json(self.path, new, on_commit=lambda: setattr(self, 'state', new))
            except OSError as error:
                self.durability_failed = True
                raise Rejected('durability_error') from error
        self.ack = local(self.local['socket'], dict(op='apply', desired=value))
        if old != new:
            audit('desired_received', host=self.cfg['host'], generation=new['generation'],
                  kernel_verified=self.ack['verified'])

    def tick(self):
        if self.session is None:
            caps = self.capabilities
            try:
                reply = self.call('hello', boot_id=self.boot, capabilities=caps)
            except Rejected as error:
                if error.code != 'capability_mismatch':
                    raise
                caps = [cap for cap in caps if cap in ('telemetry_v1', 'cap_v1')]
                if caps == self.capabilities:
                    raise
                reply = self.call('hello', boot_id=self.boot, capabilities=caps)
            self.fleet_metrics = 'fleet_metrics_v1' in caps
            self.cooperative = 'cooperative_slo_v1' in caps
            hexdigest(reply['session'], 48)
            if self.state and reply['epoch'] != self.state['epoch']:
                raise Rejected('epoch_changed')
            self.session, self.seq = reply['session'], 0
        self.ack = None
        if self.local:
            try:
                self.ack = local(self.local['socket'], dict(op='status'))
            except (OSError, EOFError, ValueError):
                pass
        self.seq += 1
        data = self.collector.sample()
        if self.cooperative:
            from cooperative_slo import read_aggregate
            source = self.cfg['cooperative']
            try:
                data['cooperative'] = read_aggregate(source['summary_path'], source['service'])
            except (OSError, ValueError, TypeError, KeyError):
                pass
        if not self.fleet_metrics:
            for key in ('sample_end_ns', 'controller_actions', 'controller_session'):
                data.pop(key, None)
        try:
            reply = self.call('heartbeat', session=self.session, seq=self.seq,
                              telemetry=data, ack=self.ack)
        except Rejected as error:
            if error.code == 'unknown_session':
                self.session = None
            raise
        if reply['desired'] is not None:
            self.apply(reply['desired'])
        path = self.cfg['cooperative'].get('advice_path') if self.cooperative else None
        if path and self.ack and self.ack['verified'] and self.ack['state']=='applied':
            try:
                atomic_json(Path(path), dict(sample_ns=time.monotonic_ns(), cap_kbit=self.ack['rate_kbit'],
                                            observed_kbit=data['egress_kbit']))
            except (OSError, ValueError) as error:
                audit('cooperative_advice_error', host=self.cfg['host'], reason=type(error).__name__)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        if os.geteuid() == 0:
            raise PermissionError('run the Fabric agent as an unprivileged account')
        with locked_state(args.state):
            agent = Agent(read_json(args.config), args.state / 'accepted.json')
            interval = agent.cfg['poll_ms']/1000
            audit('agent_ready', host=agent.cfg['host'], capabilities=agent.capabilities)
            previous = None
            failures = 0
            while not stop.is_set():
                start = time.monotonic()
                try:
                    agent.tick()
                    error = None
                except (OSError, EOFError, ValueError, KeyError, TypeError, RuntimeError) as failure:
                    error = getattr(failure, 'code', type(failure).__name__)
                if error != previous:
                    audit('agent_link', host=agent.cfg['host'], error=error)
                previous = error
                failures = failures+1 if error else 0
                stop.wait(max(0, min(2, interval*2**min(failures, 2)) - (time.monotonic()-start)))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'fabric agent: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
