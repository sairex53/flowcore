#!/usr/bin/env python3
"""Exercise Fabric over real TLS and tc on three senders, a router and a receiver."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import signal
import ssl
import subprocess
import sys
import tempfile
import time

from benchmark import GID, UID, Lab, owned_directory
from paths import BINDIR, ROOT, TC

HOSTS = ('a', 'b', 'c')
ROUTER = 3
SERVER = 4
CAPACITY = 6000
PORT = 19443
APPS = ROOT / 'examples/real_apps'


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')


def save_user(path, value):
    save(path, value)
    os.chown(path, UID, GID)
    path.chmod(0o600)


def stop(process, abrupt=False):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL if abrupt else signal.SIGTERM)
    process.wait(timeout=12)


def until(callback, label, timeout=12):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = callback()
            if value:
                return value
        except (OSError, subprocess.SubprocessError) as error:
            last = str(error)
        time.sleep(.1)
    raise RuntimeError(f'timed out waiting for {label}: {last}')


def openssl(*args):
    subprocess.run(['openssl', *map(str, args)], check=True, capture_output=True, timeout=10)


def private_pki(directory):
    """Short-lived test identities. Parent cleanup removes all private keys."""
    ca, key = directory / 'ca.pem', directory / 'ca.key'
    openssl('req', '-x509', '-newkey', 'ed25519', '-noenc', '-days', 1, '-subj',
            '/CN=FlowcoreFixtureCA', '-out', ca, '-keyout', key, '-addext',
            'basicConstraints=critical,CA:TRUE')
    identities, pins = {}, {}
    for name in ('coordinator', 'operator', 'other', *HOSTS):
        crt, private = directory / f'{name}.pem', directory / f'{name}.key'
        csr, ext = directory / 'request.csr', directory / 'extensions'
        ext.write_text('basicConstraints=critical,CA:FALSE\n' + (
            'extendedKeyUsage=serverAuth\nsubjectAltName=DNS:fabric.test\n' if name == 'coordinator'
            else 'extendedKeyUsage=clientAuth\n'))
        openssl('req', '-newkey', 'ed25519', '-noenc', '-subj', f'/CN={name}', '-keyout', private, '-out', csr)
        openssl('x509', '-req', '-in', csr, '-CA', ca, '-CAkey', key, '-CAcreateserial',
                '-days', 1, '-extfile', ext, '-out', crt)
        pins[name] = hashlib.sha256(ssl.PEM_cert_to_DER_cert(crt.read_text())).hexdigest()
        ca.chmod(0o644)
        crt.chmod(0o644)
        os.chown(private, UID, GID)
        private.chmod(0o600)
        identities[name] = dict(ca=str(ca), cert=str(crt), key=str(private))
    return identities, pins


def setup(lab):
    lab.start_holders(5)
    for index in range(5):
        lab.command(index, ['ip', 'link', 'set', 'lo', 'up'])
    for index in (*range(3), SERVER):
        suffix = index + 1 if index < 3 else 4
        router_dev = f'd{suffix}'
        lab.command(ROUTER, ['ip', 'link', 'add', router_dev, 'type', 'veth', 'peer',
                             'name', 'data0', 'netns', lab.namespace_path(index)])
        for namespace, dev, ip in ((ROUTER, router_dev, f'198.18.{suffix}.1/30'),
                                   (index, 'data0', f'198.18.{suffix}.2/30')):
            lab.command(namespace, ['ip', 'addr', 'add', ip, 'dev', dev])
            lab.command(namespace, ['ip', 'link', 'set', dev, 'up'])
        lab.command(index, ['ip', 'route', 'add', '198.18.0.0/16', 'via', f'198.18.{suffix}.1'])
    for index in range(3):
        suffix = index + 1
        router_dev = f'm{suffix}'
        lab.command(ROUTER, ['ip', 'link', 'add', router_dev, 'type', 'veth', 'peer',
                             'name', 'ctrl0', 'netns', lab.namespace_path(index)])
        for namespace, dev, ip in ((ROUTER, router_dev, f'192.0.2.{4*index+1}/30'),
                                   (index, 'ctrl0', f'192.0.2.{4*index+2}/30')):
            lab.command(namespace, ['ip', 'addr', 'add', ip, 'dev', dev])
            lab.command(namespace, ['ip', 'link', 'set', dev, 'up'])
        lab.command(index, ['ip', 'link', 'set', 'data0', 'alias', 'flowcore-lab:fabric-test'])
    lab.command(ROUTER, ['sysctl', '-q', '-w', 'net.ipv4.ip_forward=1'])
    tc = [TC, 'qdisc', 'add', 'dev', 'd4']
    lab.command(ROUTER, [*tc, 'root', 'handle', '1:', 'htb', 'default', '20'])
    lab.command(ROUTER, [TC, 'class', 'add', 'dev', 'd4', 'parent', '1:', 'classid', '1:20',
                         'htb', 'rate', f'{CAPACITY}kbit', 'ceil', f'{CAPACITY}kbit'])
    lab.command(ROUTER, [*tc, 'parent', '1:20', 'handle', '20:', 'fq_codel'])
    rows = json.loads(lab.command(ROUTER, [TC, '-j', 'class', 'show', 'dev', 'd4']))
    assert any(row.get('handle') == '1:20' and row['rate'] == CAPACITY*125 and
               row['ceil'] == CAPACITY*125 for row in rows)
    return rows


def endpoint(host):
    return dict(host=host, port=PORT, server_name='fabric.test')


def user_dir(path):
    path.mkdir(mode=0o700)
    os.chown(path, UID, GID)
    return path


def process_usage(proc):
    raw = Path(f'/proc/{proc.pid}/stat').read_text().rsplit(')', 1)[1].split()
    status = Path(f'/proc/{proc.pid}/status').read_text().splitlines()
    rss = next(int(row.split()[1])*1024 for row in status if row.startswith('VmRSS:'))
    return dict(cpu_seconds=(int(raw[11])+int(raw[12]))/os.sysconf('SC_CLK_TCK'), rss_bytes=rss)


class Experiment:
    def __init__(self, lab, private, mode, seed):
        self.lab, self.private, self.mode, self.seed = lab, private, mode, seed
        self.agents, self.daemons, self.actuators = {}, {}, {}
        self.coordinator = None
        self.fleet_config = None
        self.floor = 500 if mode == 'fabric' else 2000
        self.identities, self.pins = private_pki(private)
        self.coordinator_config = private / 'coordinator.json'
        self.operator_config = private / 'operator.json'
        save(self.operator_config, dict(coordinator=endpoint('127.0.0.1'), tls=self.identities['operator']))
        self.coordinator_state = user_dir(private / 'coordinator-state')
        self.sockets = private / 'act'
        self.sockets.mkdir(mode=0o755)
        self.sockets.chmod(0o755)
        self.ceiling = 5000 if mode == 'fabric' else 2000

    def cli(self, op, *args):
        return json.loads(self.lab.command(ROUTER, [BINDIR/'flowcore', 'fabric', op,
                          '--config', self.operator_config, '--json', *args]))

    def data_cap(self, index):
        rows = json.loads(self.lab.command(index, [TC, '-j', 'class', 'show', 'dev', 'data0']))
        row = next((q for q in rows if q.get('handle') == 'fc01:20'), None)
        return row['rate']//125 if row else None

    def verify(self):
        return all(self.cli('policies')['verified'].values())

    def coordinator_start(self, demands):
        agents = {host: dict(pins=[self.pins[host]], environment='lab',
            groups=['request' if host == 'a' else 'batch'],
            services={('api' if host == 'a' else 'batch'): ('critical' if host == 'a' else 'standard')},
            egress=dict(resource='uplink', interface='data0', floor_kbit=self.floor, max_kbit=self.ceiling))
            for host in HOSTS}
        intents = [dict(id=f'{host}-traffic', match=dict(hosts=[host], environment='lab'),
                        precedence=10, weight=4 if host == 'a' else 1, desired_kbit=rate,
                        target_p99_us=100000)
                   for host, rate in zip(HOSTS, demands)]
        coordinator_config = dict(cluster='fabric-lab', listen=dict(host='0.0.0.0', port=PORT),
            tls=self.identities['coordinator'], admins=[self.pins['operator']], agents=agents,
            resources={'uplink': dict(capacity_kbit=CAPACITY, path=['router-egress', 'receiver'])},
            intents=intents)
        if self.fleet_config is not None:
            coordinator_config['fleet'] = self.fleet_config
        save_user(self.coordinator_config, coordinator_config)
        name = 'coordinator' if self.coordinator is None else 'coordinator-restart'
        self.coordinator = self.lab.spawn(ROUTER, [sys.executable, '-B', ROOT/'scripts/fabric_coordinator.py',
                    self.coordinator_config, '--state', self.coordinator_state], name)
        until(lambda: self.cli('status'), 'mutual TLS listener')

    def agent_start(self, index, host):
        return self.lab.spawn(index, [sys.executable, '-B', ROOT/'scripts/fabric_agent.py',
               self.private/f'agent-{host}.json', '--state', self.private/f'{host}-state'],
               f'agent-{host}' if host not in self.agents else f'agent-{host}-restart')

    def start(self, coordinate=True):
        for index, host in enumerate(HOSTS):
            act = self.private/f'actuator-{host}.json'
            local = dict(resource='uplink', interface='data0', floor_kbit=self.floor, max_kbit=self.ceiling)
            sock = self.sockets/host
            save(act, dict(cluster='fabric-lab', host=host, owner='fabric-test', agent_uid=UID, agent_gid=GID, **local))
            self.actuators[host] = self.lab.spawn(index, [sys.executable, '-B', ROOT/'scripts/fabric_actuator.py', act, '--socket', sock],
                           f'actuator-{host}', privileged=True)
            until(lambda: self.data_cap(index) == self.floor, f'{host} baseline')
            runtime = user_dir(self.private/f'd-{host}')
            service = 'api' if host == 'a' else 'batch'
            daemon_cfg = json.loads((ROOT/'config/service.json').read_text())
            daemon_cfg['intent_api'] = {'priorities': ['critical' if host == 'a' else 'standard']}
            daemon_cfg['observability'].update(bpf='off', services={service: dict(
                **{'class': 'critical' if host == 'a' else 'standard'}, slo_ms=200)})
            path = self.private/f'daemon-{host}.json'
            save_user(path, daemon_cfg)
            self.daemons[host] = self.lab.spawn(index, ['env', f'FLOWCORE_RUNTIME={runtime}',
                f'FLOWCORE_STATE={runtime}', BINDIR/'flowcored', '--config', path], f'daemon-{host}')
            until(lambda: (runtime/'control.sock').is_socket(), f'{host} standalone daemon')
            user_dir(self.private/f'{host}-state')
            save_user(self.private/f'agent-{host}.json', dict(cluster='fabric-lab', host=host,
                coordinator=endpoint(f'192.0.2.{4*index+1}'), tls=self.identities[host],
                interfaces=['data0', 'ctrl0'], services=[service], poll_ms=500,
                egress=dict(local, socket=str(sock)), daemon_socket=str(runtime/'control.sock')))
        if self.mode != 'fabric':
            return None
        self.coordinator_start((4000, 1000, 1000))
        for index, host in enumerate(HOSTS):
            self.agents[host] = self.agent_start(index, host)
        until(self.verify, 'baseline readback acknowledgements')
        if not coordinate:
            return {'caps': [self.data_cap(i) for i in range(3)]}
        before = time.monotonic()
        report = self.cli('reconcile', '--apply')
        assert report['state'] == 'committed'
        until(self.verify, 'initial coordinated generation')
        caps = [self.data_cap(i) for i in range(3)]
        assert caps == [4000, 1000, 1000], caps
        return dict(convergence_seconds=time.monotonic()-before, rollout=report, caps=caps)

    def counters(self):
        samples = {}
        for host, index, device in [('shared', ROUTER, 'd4'), *[(host, i, 'data0') for i, host in enumerate(HOSTS)]]:
            stats = json.loads(self.lab.command(index, [TC, '-s', '-j', 'qdisc', 'show', 'dev', device]))
            root = next(row for row in stats if row.get('root'))
            samples[host] = dict(time_ns=time.monotonic_ns(), qdisc=root)
        return samples

    def overhead(self):
        processes = {f'actuator-{name}': proc for name, proc in self.actuators.items()}
        processes.update({f'agent-{name}': proc for name, proc in self.agents.items()})
        if self.coordinator:
            processes['coordinator'] = self.coordinator
        control = {}
        for index, host in enumerate(HOSTS):
            link, = json.loads(self.lab.command(index, ['ip', '-s', '-j', 'link', 'show', 'dev', 'ctrl0']))
            stats = link.get('stats64') or link['stats']
            control[host] = {direction: stats[direction]['bytes'] for direction in ('tx', 'rx')}
        return dict(time_ns=time.monotonic_ns(), processes={name: process_usage(proc)
                    for name, proc in processes.items()}, control=control)

    def workload(self):
        for index, port in enumerate((19000, 19100)):
            self.lab.spawn(SERVER, [sys.executable, '-B', APPS/'http_service.py', '--bind', '198.18.4.2',
                '--critical-port', port, '--background-port', port+1, '--database-port', port+2],
                f'server-{index}')
        time.sleep(.3)
        artifact = self.lab.output/'upload.bin'
        artifact.write_bytes(bytes(1024*1024))
        artifact.chmod(0o644)
        clients = []
        usage = self.overhead()
        planned = time.monotonic_ns()+2_000_000_000
        for index, host in enumerate(HOSTS):
            port = 19000 if host == 'a' else 19100
            output = self.lab.output/f'{host}.json'
            clients.append(self.lab.spawn(index, ['env',
                f'FLOWCORE_SOCKET={self.private}/d-{host}/control.sock',
                sys.executable, '-B', APPS/'http_benchmark.py', '--host', '198.18.4.2',
                '--port', port, '--background-port', port+1, '--duration', 6, '--rate', 40,
                '--deadline-ms', 200, '--concurrency', 4, '--service', 'api' if host == 'a' else 'batch',
                '--priority', 'critical' if host == 'a' else 'standard',
                '--start-ns', planned, '--seed', self.seed, '--background-bytes', 1024*1024,
                '--background-upload-file', artifact, '--output', output], f'client-{host}'))
        time.sleep(max(0, (planned+200_000_000-time.monotonic_ns())/1e9))
        first = self.counters()
        time.sleep(max(0, (planned+1_700_000_000-time.monotonic_ns())/1e9))
        second = self.counters()
        during = self.cli('diagnostics') if self.coordinator else None
        services = self.cli('services') if self.coordinator else None
        for proc in clients:
            if proc.wait(timeout=35):
                raise RuntimeError('client failed; inspect client logs')
        outputs = {host: json.loads((self.lab.output/f'{host}.json').read_text()) for host in HOSTS}
        for result in outputs.values():
            assert result['sdk']['connection'] == 'connected', result['sdk']
            assert result['background_transfer']['error'] is None
            assert result['background_transfer']['bytes_transferred'] == 1024*1024
        rates = {name: (sample['qdisc']['bytes']-first[name]['qdisc']['bytes'])*8e6 /
                 (sample['time_ns']-first[name]['time_ns']) for name, sample in second.items()}
        values = [rates[name] for name in HOSTS]
        end_usage = self.overhead()
        process_cost = {name: dict(cpu_seconds=round(after['cpu_seconds']-usage['processes'][name]['cpu_seconds'], 3),
                                   rss_bytes=after['rss_bytes']) for name, after in end_usage['processes'].items()}
        control_cost = {host: {direction: end_usage['control'][host][direction]-usage['control'][host][direction]
                              for direction in ('tx', 'rx')} for host in HOSTS}
        return dict(first=first, second=second, rates_kbit=rates, utilization=rates['shared']/CAPACITY,
                    jain=sum(values)**2/(3*sum(x*x for x in values)), agent_samples=during, services=services,
                    control_cost=dict(elapsed_seconds=(end_usage['time_ns']-usage['time_ns'])/1e9,
                                      processes=process_cost, link_bytes=control_cost),
                    requests={host: data['summary'] for host, data in outputs.items()},
                    bulk={host: data['background_transfer'] for host, data in outputs.items()},
                    sdk={host: data['sdk'] for host, data in outputs.items()})

    def data_probe(self, index):
        port = 19000 if index == 0 else 19100
        source = """import http.client,sys
with_connection = http.client.HTTPConnection('198.18.4.2', int(sys.argv[1]), timeout=2)
with_connection.request('GET', '/api/critical?work_ms=2')
response = with_connection.getresponse()
assert response.status == 200 and response.read()
with_connection.close()
"""
        self.lab.command(index, [sys.executable, '-B', '-c', source, port])

    def failure(self):
        before = self.cli('status')
        lost = time.monotonic()
        self.lab.command(2, ['ip', 'link', 'set', 'ctrl0', 'down'])
        try:
            until(lambda: self.data_cap(2) == self.floor, 'isolated agent watchdog fallback')
            fallback = time.monotonic()-lost
            until(lambda: self.cli('status')['health']['c'] == 'stale', 'missing heartbeat')
            blocked = self.cli('reconcile', '--apply')
            assert blocked['state'] == 'waiting_for_verified' and 'c' in blocked['waiting']
            self.data_probe(2)
        finally:
            self.lab.command(2, ['ip', 'link', 'set', 'ctrl0', 'up'])
        resumed = time.monotonic()
        until(self.verify, 'partition recovery')
        partition_recovery = time.monotonic()-resumed
        crashed = time.monotonic()
        stop(self.coordinator, abrupt=True)
        until(lambda: all(self.data_cap(i) == self.floor for i in range(3)), 'coordinator crash fallback')
        crash_fallback = time.monotonic()-crashed
        self.data_probe(0)
        restarted = time.monotonic()
        self.coordinator_start((1000, 4000, 1000))
        until(self.verify, 'coordinator restart reconciliation')
        recovered = self.cli('status')
        assert before['epoch'] == recovered['epoch'] and before['generation'] == recovered['generation']
        recovery = time.monotonic()-restarted
        rollout = time.monotonic()
        down = self.cli('reconcile', '--apply')
        assert down['first_step'] == dict(a=1000, b=1000, c=1000), down
        until(self.verify, 'lowering barrier')
        assert [self.data_cap(i) for i in range(3)] == [1000, 1000, 1000]
        up = self.cli('reconcile', '--apply')
        assert up['state'] == 'increases_committed'
        until(self.verify, 'raised allocation')
        assert [self.data_cap(i) for i in range(3)] == [1000, 4000, 1000]
        rollout_seconds = time.monotonic()-rollout
        stopped = time.monotonic()
        stop(self.agents['b'])
        self.agents['b'] = self.agent_start(1, 'b')
        until(lambda: self.cli('status')['counters']['hellos'] >= 4 and self.verify(), 'agent restart')
        return dict(partition_fallback_seconds=fallback, partition_recovery_seconds=partition_recovery,
                    partition_application_probe='HTTP 200', crash_application_probe='HTTP 200',
                    crash_fallback_seconds=crash_fallback, coordinator_recovery_seconds=recovery,
                    decrease=down, increase=up, rollout_seconds=rollout_seconds,
                    agent_restart_seconds=time.monotonic()-stopped)

    def security(self):
        source = """import json,sys,ssl
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1])/'scripts'))
from fabric_protocol import remote,tls,Rejected
cfg=json.loads(sys.argv[2]); modes=json.loads(sys.argv[3])
out={}
for name, cert, server, request in modes:
    try:
        remote(tls(cfg[cert]), dict(host='127.0.0.1', port=19443, server_name=server), request)
    except ssl.SSLCertVerificationError:
        out[name]='tls_identity'
    except Rejected as e:
        out[name]=e.code
    else:
        raise RuntimeError(name+' unexpectedly accepted')
print(json.dumps(out))
"""
        cases = [('wrong_server', 'operator', 'wrong.test', dict(op='status')),
                 ('unknown_pin', 'other', 'fabric.test', dict(op='status')),
                 ('impersonation', 'a', 'fabric.test', dict(op='hello', host='b', cluster='fabric-lab')),
                 ('agent_admin', 'a', 'fabric.test', dict(op='reconcile', host='a', cluster='fabric-lab', apply=True))]
        result = json.loads(self.lab.command(ROUTER, [sys.executable, '-B', '-c', source,
                          ROOT, json.dumps(self.identities), json.dumps(cases)]))
        assert result == dict(wrong_server='tls_identity', unknown_pin='unauthorized',
                              impersonation='unauthorized', agent_admin='agent_operation'), result
        return result


def run(mode, seed, directory, failures):
    owned_directory(directory)
    runtime = ROOT/'results'
    if not runtime.exists():
        owned_directory(runtime)
    lab = Lab(directory)
    with tempfile.TemporaryDirectory(prefix='fx-', dir=runtime) as name:
        private = Path(name)
        private.chmod(0o755)
        try:
            verified_shared = setup(lab)
            experiment = Experiment(lab, private, mode, seed)
            initial = experiment.start()
            workload = experiment.workload()
            result = dict(mode=mode, seed=seed, shared_readback=verified_shared,
                          initial=initial, workload=workload, failure=None, security=None)
            save(directory/'run.json', result)
            if failures and mode == 'fabric':
                result['security'] = experiment.security()
                result['failure'] = experiment.failure()
                save(directory/'run.json', result)
            return result
        finally:
            lab.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repetitions', type=int, default=2)
    parser.add_argument('--seed', type=int, default=7107)
    parser.add_argument('--mode', choices=('both', 'independent', 'fabric'), default='both')
    args = parser.parse_args()
    if (os.geteuid() != 0 or UID == 0 or not 1 <= args.repetitions <= 8
            or not 0 <= args.seed <= 2**32-9):
        parser.error('run through sudo as a nonroot operator; use 1..8 repetitions and a 32-bit seed')
    args.output = args.output.resolve()
    owned_directory(args.output)
    rng = random.Random(args.seed)
    order = []
    for repetition in range(args.repetitions):
        modes = ['independent', 'fabric'] if args.mode == 'both' else [args.mode]
        rng.shuffle(modes)
        order.extend(dict(mode=mode, seed=args.seed+repetition, repetition=repetition) for mode in modes)
    save(args.output/'order.json', order)
    save(args.output/'environment.json', dict(kernel=os.uname().release, architecture=os.uname().machine,
        boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(), cpus=os.cpu_count(),
        revision=subprocess.check_output(['git', '-C', ROOT, 'rev-parse', 'HEAD'], text=True).strip(),
        dirty=bool(subprocess.check_output(['git', '-C', ROOT, 'status', '--porcelain'], text=True))))
    reports = []
    for row in order:
        label = f"{row['mode']}-{row['repetition']:02d}"
        reports.append(run(row['mode'], row['seed'], args.output/label, row['repetition']==0))
        save(args.output/'results.json', reports)
        print(f'{label}: complete', flush=True)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    main()
