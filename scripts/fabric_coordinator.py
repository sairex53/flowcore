"""Optional Fabric coordinator. Network policy is expressed only as scoped egress caps."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time

from durable_state import atomic_json, read_json
from fabric_protocol import (CLASSES, MAX_AGENTS, Rejected, audit, boolean, bounded_list,
                             fields, hexdigest, ident, integer, locked_state, serve_tls, tls)

MAX_RATE = 1_000_000
MAX_GENERATION = 2**53 - 1
STALE_SECONDS = 4
LEASE_MS = 3000


def names(value, limit=8):
    items = [ident(item) for item in bounded_list(value, limit)]
    if len(set(items)) != len(items):
        raise ValueError('duplicate identifiers')
    return items


def pins(value):
    values = [hexdigest(pin) for pin in bounded_list(value, 4)]
    if not values or len(set(values)) != len(values):
        raise ValueError('pins must be nonempty and unique')
    return values


def config(value):
    fields(value, ('cluster', 'listen', 'tls', 'admins', 'agents', 'resources', 'intents'), ('fleet', 'cooperative'))
    ident(value['cluster'])
    fields(value['listen'], ('host', 'port'))
    if not isinstance(value['listen']['host'], str) or not 1 <= len(value['listen']['host']) <= 253:
        raise ValueError('invalid listen address')
    integer(value['listen']['port'], 1, 65535)
    used_pins = set(pins(value['admins']))
    agents = value['agents']
    resources = value['resources']
    if not isinstance(agents, dict) or not 1 <= len(agents) <= MAX_AGENTS:
        raise ValueError('invalid enrollment size')
    if not isinstance(resources, dict) or len(resources) > 8:
        raise ValueError('invalid resource inventory')
    for resource, row in resources.items():
        ident(resource)
        fields(row, ('capacity_kbit', 'path'))
        integer(row['capacity_kbit'], 100, MAX_RATE)
        names(row['path'])
    floors = dict.fromkeys(resources, 0)
    for agent, row in agents.items():
        ident(agent)
        fields(row, ('pins', 'environment', 'groups', 'services'), ('egress',))
        ident(row['environment'])
        names(row['groups'])
        certs = set(pins(row['pins']))
        if certs & used_pins:
            raise ValueError('certificate pin assigned to multiple principals')
        used_pins |= certs
        services = row['services']
        if not isinstance(services, dict) or len(services) > 8:
            raise ValueError('invalid service inventory')
        for name, cls in services.items():
            ident(name)
            if cls not in CLASSES:
                raise ValueError('unknown traffic class')
        if 'egress' in row:
            egress = fields(row['egress'], ('resource', 'interface', 'floor_kbit', 'max_kbit'))
            resource = egress['resource']
            if resource not in resources:
                raise ValueError('unknown shared resource')
            ident(egress['interface'], 15)
            floor = integer(egress['floor_kbit'], 100, resources[resource]['capacity_kbit'])
            integer(egress['max_kbit'], floor, resources[resource]['capacity_kbit'])
            floors[resource] += floor
    if any(floor > resources[name]['capacity_kbit'] for name, floor in floors.items()):
        raise ValueError('protected floors exceed shared capacity')
    seen = set()
    for rule in bounded_list(value['intents'], 32):
        fields(rule, ('id', 'match', 'precedence', 'weight', 'desired_kbit', 'target_p99_us'))
        name = ident(rule['id'])
        if name in seen:
            raise ValueError('duplicate intent id')
        seen.add(name)
        selector = fields(rule['match'], (), ('hosts', 'services', 'groups', 'environment', 'class'))
        for key in ('hosts', 'services', 'groups'):
            if key in selector and not names(selector[key], MAX_AGENTS):
                raise ValueError('empty intent selector')
        if 'environment' in selector:
            ident(selector['environment'])
        if 'class' in selector and selector['class'] not in CLASSES:
            raise ValueError('invalid class selector')
        integer(rule['precedence'], 0, 1000)
        integer(rule['weight'], 1, 32)
        integer(rule['desired_kbit'], 0, MAX_RATE)
        integer(rule['target_p99_us'], 1, 60_000_000)
    if 'cooperative' in value:
        policy = fields(value['cooperative'], ('version', 'minimum_offered', 'risk_percent', 'boost', 'services'))
        integer(policy['version'], 1, 1)
        integer(policy['minimum_offered'], 8, 1000)
        integer(policy['risk_percent'], 10, 100)
        integer(policy['boost'], 1, 2)
        selected = policy['services']
        if not isinstance(selected, dict) or not 1 <= len(selected) <= len(agents):
            raise ValueError('invalid cooperative enrollment')
        for host, name in selected.items():
            if (host not in agents or 'egress' not in agents[host] or
                    agents[host]['services'].get(ident(name)) not in ('critical', 'interactive')):
                raise ValueError('cooperative influence requires an enrolled egress service')
    return value


def telemetry(value):
    fields(value, ('age_ms', 'interfaces', 'services', 'daemon_healthy', 'controller', 'bottleneck',
                   'egress_kbit', 'decision'), ('sample_end_ns', 'controller_actions', 'controller_session', 'cooperative'))
    if 'cooperative' in value:
        from cooperative_slo import aggregate
        aggregate(value['cooperative'])
    for key in ('sample_end_ns', 'controller_actions'):
        if value.get(key) is not None:
            integer(value[key], 0, 2**63-1)
    if value.get('controller_session') is not None:
        ident(value['controller_session'])
    integer(value['age_ms'], 0, 120000)
    if value['daemon_healthy'] is not None:
        boolean(value['daemon_healthy'])
    for key in ('controller', 'bottleneck', 'decision'):
        if value[key] is not None:
            ident(value[key])
    if value['egress_kbit'] is not None:
        integer(value['egress_kbit'], 0, MAX_RATE * 100)
    seen = set()
    for row in bounded_list(value['interfaces'], 8):
        fields(row, ('name', 'mtu', 'up'))
        name = ident(row['name'], 15)
        if name in seen:
            raise ValueError('duplicate interface')
        seen.add(name)
        integer(row['mtu'], 1, 1_000_000)
        boolean(row['up'])
    seen = set()
    for row in bounded_list(value['services'], 8):
        fields(row, ('name', 'samples', 'errors', 'misses', 'p99_us', 'window_seconds'))
        name = ident(row['name'])
        if name in seen:
            raise ValueError('duplicate service sample')
        seen.add(name)
        for key in ('samples', 'errors', 'misses'):
            integer(row[key], 0, 10_000_000)
        integer(row['window_seconds'], 1, 3600)
        if row['p99_us'] is not None:
            integer(row['p99_us'], 0, 600_000_000)
    return value


def acknowledgement(value):
    if value is not None:
        fields(value, ('epoch', 'generation', 'resource', 'rate_kbit', 'verified', 'state'))
        if value['epoch'] is not None:
            hexdigest(value['epoch'], 32)
        integer(value['generation'], 0, MAX_GENERATION)
        ident(value['resource'])
        integer(value['rate_kbit'], 0, MAX_RATE)
        boolean(value['verified'])
        if value['state'] not in ('applied', 'fallback', 'error'):
            raise ValueError('invalid actuation state')
    return value


def matches(selector, host, row, service, cls):
    return (host in selector.get('hosts', [host]) and service in selector.get('services', [service])
            and ('groups' not in selector or set(selector['groups']) & set(row['groups']))
            and row['environment'] == selector.get('environment', row['environment'])
            and cls == selector.get('class', cls))


def allocate(capacity, bounds):
    """Deterministic integral weighted max-min over demand above protected floors."""
    result = {host: floor for host, (floor, _, _) in bounds.items()}
    remaining = capacity - sum(result.values())
    if remaining < 0:
        raise ValueError('infeasible floors')
    while remaining:
        eligible = [host for host in sorted(bounds) if result[host] < bounds[host][1]]
        if not eligible:
            break
        total = sum(bounds[host][2] for host in eligible)
        additions = {host: min(bounds[host][1] - result[host], remaining * bounds[host][2] // total)
                     for host in eligible}
        if not any(additions.values()):
            for host in sorted(eligible, key=lambda host: (-bounds[host][2], host))[:remaining]:
                additions[host] = 1
        for host, addition in additions.items():
            result[host] += addition
            remaining -= addition
    return result


class Coordinator:
    def __init__(self, cfg, state_path, clock=time.monotonic):
        self.cfg, self.path, self.clock = config(cfg), state_path, clock
        self.lock = threading.Lock()
        self.sessions = {}
        self.auth = {pin: host for host, row in cfg['agents'].items() for pin in row['pins']}
        self.egress = {host: row['egress'] for host, row in cfg['agents'].items() if 'egress' in row}
        scope = [cfg['cluster'], self.egress, {key: row['capacity_kbit'] for key, row in cfg['resources'].items()}]
        self.scope = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
        self.durability_failed = False
        self.counters = dict(hellos=0, reports=0, duplicates=0, rejected=0, reconciles=0)
        try:
            self.state = read_json(state_path)
        except FileNotFoundError:
            self.state = dict(epoch=secrets.token_hex(16), scope=self.scope, generation=1, pending=None,
                              quotas={host: row['floor_kbit'] for host, row in self.egress.items()})
            atomic_json(self.path, self.state)
        self.validate_state(self.state)
        self.fleet = None
        if 'fleet' in cfg:
            from fleet_rollout import Fleet
            self.fleet = Fleet(self, cfg['fleet'])
        elif 'fleet' in self.state:
            raise ValueError('Fleet state requires Fleet authority configuration')

    def validate_quotas(self, quotas):
        fields(quotas, self.egress)
        totals = dict.fromkeys(self.cfg['resources'], 0)
        for host, cap in quotas.items():
            row = self.egress[host]
            totals[row['resource']] += integer(cap, row['floor_kbit'], row['max_kbit'])
        if any(total > self.cfg['resources'][resource]['capacity_kbit'] for resource, total in totals.items()):
            raise ValueError('allocation exceeds shared capacity')

    def validate_state(self, state):
        fields(state, ('epoch', 'scope', 'generation', 'pending', 'quotas'), ('fleet',))
        hexdigest(state['epoch'], 32)
        if state['scope'] != self.scope:
            raise ValueError('enrollment resource scope changed; drain and re-enroll explicitly')
        integer(state['generation'], 1, MAX_GENERATION)
        self.validate_quotas(state['quotas'])
        if state['pending'] is not None:
            self.validate_quotas(state['pending'])
            if any(state['quotas'][host] > target for host, target in state['pending'].items()):
                raise ValueError('pending allocation would decrease a committed cap')

    def persist(self, state):
        self.validate_state(state)
        try:
            atomic_json(self.path, state, on_commit=lambda: setattr(self, 'state', state))
        except OSError as error:
            self.durability_failed = True
            audit('durability_error', generation=self.state['generation'])
            raise Rejected('durability_error') from error

    def commit(self, quotas, pending):
        state = dict(self.state, generation=self.state['generation'] + 1, quotas=quotas, pending=pending)
        self.persist(state)
        audit('generation_committed', generation=state['generation'], quotas=quotas,
              pending_increases=pending is not None)

    def fresh(self, row):
        return row is not None and row['report'] is not None and self.clock() - row['received'] <= STALE_SECONDS

    def verified(self, host):
        row = self.sessions.get(host)
        if not self.fresh(row) or 'cap_v1' not in row['capabilities']:
            return False
        ack = row['report']['ack']
        return bool(ack and ack['verified'] and ack['state'] == 'applied' and
                    ack['epoch'] == self.state['epoch'] and ack['generation'] == self.state['generation'] and
                    ack['resource'] == self.egress[host]['resource'] and
                    ack['rate_kbit'] == self.state['quotas'][host])

    def desired(self, host, row):
        if self.durability_failed or host not in self.egress or 'cap_v1' not in row['capabilities']:
            return None
        return dict(cluster=self.cfg['cluster'], host=host, epoch=self.state['epoch'],
                    resource=self.egress[host]['resource'], interface=self.egress[host]['interface'],
                    generation=self.state['generation'], rate_kbit=self.state['quotas'][host], lease_ms=LEASE_MS)

    def hello(self, host, message):
        fields(message, ('version', 'op', 'cluster', 'host', 'boot_id', 'capabilities'))
        if not isinstance(message['boot_id'], str) or len(message['boot_id']) not in (32, 36):
            raise ValueError('invalid boot identity')
        hexdigest(message['boot_id'].replace('-', ''), 32)
        caps = names(message['capabilities'])
        if set(caps) - {'telemetry_v1', 'cap_v1', 'fleet_metrics_v1', 'cooperative_slo_v1'} or 'telemetry_v1' not in caps:
            raise Rejected('capability_mismatch')
        self.sessions[host] = dict(token=secrets.token_hex(24), seq=0, report=None,
                                   capabilities=caps, boot_id=message['boot_id'], received=None)
        self.counters['hellos'] += 1
        audit('agent_registered', host=host, capabilities=caps)
        return dict(session=self.sessions[host]['token'], epoch=self.state['epoch'], lease_ms=LEASE_MS)

    def report(self, host, message):
        fields(message, ('version', 'op', 'cluster', 'host', 'session', 'seq', 'telemetry', 'ack'))
        row = self.sessions.get(host)
        if not row or row['token'] != message['session']:
            raise Rejected('unknown_session')
        seq = integer(message['seq'], 1, MAX_GENERATION)
        if seq <= row['seq']:
            self.counters['duplicates'] += 1
            raise Rejected('replay')
        data = telemetry(message['telemetry'])
        ack = acknowledgement(message['ack'])
        if ack is not None and (host not in self.egress or ack['resource'] != self.egress[host]['resource']):
            raise Rejected('ack_scope')
        allowed = self.cfg['agents'][host]['services']
        if any(service['name'] not in allowed for service in data['services']):
            raise Rejected('service_scope')
        if 'cooperative' in data and ('cooperative_slo_v1' not in row['capabilities'] or
                                       data['cooperative']['service'] not in allowed):
            raise Rejected('cooperative_scope')
        old = row['report']
        row.update(seq=seq, received=self.clock(), report=dict(telemetry=data, ack=ack))
        self.counters['reports'] += 1
        if ack is not None and (old is None or old['ack'] != ack):
            audit('agent_ack', host=host, **ack)
        return dict(desired=self.desired(host, row))

    def cooperative_pressure(self, host, data, observed_rate):
        policy = self.cfg.get('cooperative')
        row = data.get('cooperative')
        if not policy or not row or policy['services'].get(host) != row['service']:
            return False
        from cooperative_slo import STALE_US
        age = row['age_us'] + data['age_ms']*1000 + int((self.clock()-self.sessions[host]['received'])*1e6)
        return (0 <= age <= STALE_US and observed_rate is not None and
                observed_rate*5 >= self.state['quotas'][host]*4 and
                row['offered'] >= policy['minimum_offered'] and
                max(row['risk'], row['misses'])*100 >= policy['risk_percent']*row['offered'])

    def plan(self):
        rules = sorted(self.cfg['intents'], key=lambda rule: (-rule['precedence'], rule['id']))
        allocations, detail = {}, {}
        for resource, shared in self.cfg['resources'].items():
            bounds = {}
            for host, local in self.egress.items():
                if local['resource'] != resource:
                    continue
                identity = self.cfg['agents'][host]
                data = self.sessions[host]['report']['telemetry']
                samples = {row['name']: row for row in data['services']} if data['age_ms'] <= 3000 else {}
                offered, weight, selected = 0, 1, {}
                for name, cls in (identity['services'] or {None: None}).items():
                    rule = next((r for r in rules if matches(r['match'], host, identity, name, cls)), None)
                    if rule is None:
                        continue
                    observed = samples.get(name)
                    missed = bool(observed and observed['samples'] >= 20 and observed['p99_us'] is not None and
                                  observed['p99_us'] > rule['target_p99_us'])
                    offered += rule['desired_kbit']
                    weight = max(weight, rule['weight'] * (2 if missed else 1))
                    selected[str(name)] = dict(rule=rule['id'], slo_pressure=missed)
                observed_rate = data['egress_kbit'] if data['age_ms'] <= 3000 else None
                pressure = self.cooperative_pressure(host, data, observed_rate)
                if pressure:
                    weight = min(64, weight*self.cfg['cooperative']['boost'])
                demand = max(local['floor_kbit'], offered, observed_rate or 0)
                bounds[host] = (local['floor_kbit'], min(local['max_kbit'], demand), weight)
                detail[host] = dict(intent=selected, demand_kbit=demand, observed_kbit=observed_rate,
                                    floor_kbit=bounds[host][0], bounded_demand_kbit=bounds[host][1], weight=weight, cooperative_pressure=pressure)
            allocations.update(allocate(shared['capacity_kbit'], bounds))
        return allocations, detail

    def reconcile(self, apply):
        self.counters['reconciles'] += 1
        if self.fleet and apply:
            raise Rejected('fleet_owns_rollouts')
        if self.durability_failed:
            raise Rejected('durability_error')
        waiting = [host for host in self.egress if not self.verified(host)]
        if waiting:
            return dict(state='waiting_for_verified', waiting=waiting, generation=self.state['generation'])
        pending = self.state['pending']
        if pending is not None:
            if apply:
                self.commit(pending, None)
            return dict(state='increases_committed' if apply else 'increases_ready',
                        generation=self.state['generation'], target=pending)
        target, detail = self.plan()
        if target == self.state['quotas']:
            return dict(state='converged', generation=self.state['generation'], target=target, evidence=detail)
        lower = {host: min(rate, self.state['quotas'][host]) for host, rate in target.items()}
        if apply:
            self.commit(lower if lower != self.state['quotas'] else target,
                        target if lower != self.state['quotas'] and lower != target else None)
        return dict(state='committed' if apply else 'preview', generation=self.state['generation'],
                    target=target, first_step=lower, evidence=detail)

    def view(self, op):
        agents = {}
        for host, enrollment in self.cfg['agents'].items():
            row = self.sessions.get(host)
            report = row['report'] if row else None
            agents[host] = dict(health='fresh' if self.fresh(row) else 'stale' if row else 'missing',
                                capabilities=row['capabilities'] if row else [],
                                boot_id=row['boot_id'] if row else None,
                                environment=enrollment['environment'], groups=enrollment['groups'],
                                egress=enrollment.get('egress'), kernel_verified=host in self.egress and self.verified(host),
                                telemetry=report['telemetry'] if report else None,
                                ack=report['ack'] if report else None,
                                receive_age_ms=int(1000*(self.clock()-row['received'])) if report else None)
        if op == 'agents':
            return agents
        if op == 'services':
            by_host, aggregate = {}, {}
            for host, row in self.cfg['agents'].items():
                node = agents[host]
                data = node['telemetry']
                samples = data['services'] if node['health'] == 'fresh' and data['age_ms'] <= 3000 else []
                by_host[host] = dict(inventory=row['services'], samples=samples)
                for name in row['services']:
                    aggregate.setdefault(name, dict(expected_hosts=[], sampled_hosts=[],
                                                    worst_host_p99_us=None))['expected_hosts'].append(host)
                for sample in samples:
                    totals = aggregate[sample['name']]
                    if sample['samples'] or sample['errors']:
                        totals['sampled_hosts'].append(host)
                    if sample['p99_us'] is not None:
                        totals['worst_host_p99_us'] = max(totals['worst_host_p99_us'] or 0, sample['p99_us'])
            return dict(by_host=by_host, aggregate=aggregate)
        if op == 'policies':
            return dict(desired=self.state, intents=self.cfg['intents'],
                        verified={host: row['kernel_verified'] for host, row in agents.items()})
        result = dict(cluster=self.cfg['cluster'], generation=self.state['generation'], epoch=self.state['epoch'],
                      health={host: row['health'] for host, row in agents.items()}, counters=self.counters.copy(),
                      pending_increases=self.state['pending'] is not None, durability_failed=self.durability_failed)
        if op == 'diagnostics':
            shared_egress = {}
            for name, resource in self.cfg['resources'].items():
                rates, missing = [], []
                for host, local in self.egress.items():
                    if local['resource'] != name:
                        continue
                    node = agents[host]
                    data = node['telemetry']
                    if node['health'] != 'fresh' or data['age_ms'] > 3000 or data['egress_kbit'] is None:
                        missing.append(host)
                    else:
                        rates.append(data['egress_kbit'])
                shared_egress[name] = dict(observed_kbit=sum(rates) if rates else None, missing=missing,
                    utilization=sum(rates)/resource['capacity_kbit'] if rates and not missing else None)
            result.update(agents=agents, resources=self.cfg['resources'], shared_egress=shared_egress)
        return result

    def tick(self):
        if self.fleet:
            with self.lock:
                try:
                    self.fleet.tick()
                except Rejected as error:
                    audit('fleet_tick_rejected', code=error.code)

    def dispatch(self, pin, message):
        if message['op'].startswith('fleet_'):
            if self.fleet is None or pin not in self.cfg['admins']:
                raise Rejected('fleet_unavailable')
            try:
                return self.fleet.dispatch(pin, message)
            except (ValueError, KeyError, TypeError) as error:
                audit('fleet_request_rejected', operation=message['op'],
                      code=getattr(error, 'code', 'invalid_request'))
                raise
        with self.lock:
            try:
                op = message['op']
                if pin in self.cfg['admins']:
                    fields(message, ('version', 'op'), ('apply',) if op == 'reconcile' else ())
                    if op == 'reconcile':
                        return self.reconcile(boolean(message.get('apply', False)))
                    if op in ('status', 'agents', 'services', 'policies', 'diagnostics'):
                        return self.view(op)
                    raise Rejected('admin_operation')
                host = self.auth.get(pin)
                if not host or host != message.get('host') or message.get('cluster') != self.cfg['cluster']:
                    raise Rejected('unauthorized')
                if op == 'hello':
                    return self.hello(host, message)
                if op == 'heartbeat':
                    return self.report(host, message)
                raise Rejected('agent_operation')
            except (ValueError, KeyError, TypeError) as error:
                self.counters['rejected'] += 1
                if self.counters['rejected'] <= 8 or self.counters['rejected'] % 100 == 0:
                    audit('request_rejected', code=getattr(error, 'code', 'invalid_request'))
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    try:
        if os.geteuid() == 0:
            raise PermissionError('run the Fabric coordinator as an unprivileged account')
        cfg = read_json(args.config)
        with locked_state(args.state):
            coordinator = Coordinator(cfg, args.state / 'desired.json')
            context = tls(cfg['tls'], server=True)
            listen = cfg['listen']
            with socket.create_server((listen['host'], listen['port'])) as listener:
                audit('coordinator_ready', cluster=cfg['cluster'], generation=coordinator.state['generation'])
                serve_tls(listener, context, coordinator.dispatch, coordinator.tick)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'fabric coordinator: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
