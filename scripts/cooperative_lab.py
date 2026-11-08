"""Run the cooperative SLO benchmark on the owned Fabric veth topology."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import tempfile
import time

from benchmark import GID, Lab, UID, owned_directory
from fabric_lab import (CAPACITY, HOSTS, ROUTER, SERVER, Experiment, endpoint, private_pki,
                        save, save_user, setup, stop, until, user_dir)
from paths import ROOT
from workload import percentile

MODES = ('no-flowcore', 'independent', 'fabric', 'cooperative')
ROLES = ('gateway', 'backend', 'database')
INDEX = dict(gateway=0, backend=1, database=SERVER)
ADDR = dict(gateway='127.0.0.1', backend='198.18.2.2', database='198.18.4.2')
PORTS = dict(gateway=19000, backend=19001, database=19002)
SERVICES = dict(a='gateway', b='backend', c='bulk')
APP = ROOT/'scripts/cooperative_app.py'


class Trial(Experiment):
    def __init__(self, lab, private, mode, seed):
        self.lab, self.private, self.mode, self.seed = lab, private, mode, seed
        self.agents, self.actuators = {}, {}
        self.coordinator = None
        self.app_processes = {}
        self.floor = 800 if mode in ('fabric', 'cooperative') else CAPACITY//len(HOSTS)
        self.ceiling = 4400 if mode in ('fabric', 'cooperative') else self.floor
        self.identities, self.pins = private_pki(private, ('client', *ROLES))
        self.coordinator_config = private/'coordinator.json'
        self.operator_config = private/'operator.json'
        save(self.operator_config, dict(coordinator=endpoint('127.0.0.1'), tls=self.identities['operator']))
        self.coordinator_state = user_dir(private/'coordinator-state')
        self.sockets = private/'act'
        self.sockets.mkdir(mode=0o755)
        self.sockets.chmod(0o755)
        self.summaries = {role: user_dir(private/role)/'sample.json' for role in ROLES}

    def coordinator_start(self):
        agents = {host: dict(pins=[self.pins[host]], environment='lab',
            groups=['service' if host != 'c' else 'background'],
            services={SERVICES[host]: 'critical' if host != 'c' else 'background'},
            egress=dict(resource='uplink', interface='data0', floor_kbit=self.floor, max_kbit=self.ceiling))
            for host in HOSTS}
        intents = [dict(id=f'{host}-traffic', match=dict(hosts=[host], environment='lab'),
                        precedence=10, weight=2 if host != 'c' else 1,
                        desired_kbit=self.ceiling, target_p99_us=200_000)
                   for host in HOSTS]
        cfg = dict(cluster='cooperative-lab', listen=dict(host='0.0.0.0', port=19443),
                   tls=self.identities['coordinator'], admins=[self.pins['operator']], agents=agents,
                   resources={'uplink': dict(capacity_kbit=CAPACITY, path=['router-egress', 'database'])},
                   intents=intents)
        if self.mode == 'cooperative':
            cfg['cooperative'] = dict(version=1, minimum_offered=12, risk_percent=25, boost=2,
                                      services={host: SERVICES[host] for host in ('a','b')})
        save_user(self.coordinator_config, cfg)
        label = 'coordinator-restart' if self.coordinator else 'coordinator'
        self.coordinator = self.lab.spawn(ROUTER, [sys.executable, '-B', ROOT/'scripts/fabric_coordinator.py',
                    self.coordinator_config, '--state', self.coordinator_state], label)
        until(lambda: self.cli('status'), 'coordinator mTLS')

    def setup_control(self):
        if self.mode == 'no-flowcore':
            return None
        for index, host in enumerate(HOSTS):
            local = dict(resource='uplink', interface='data0', floor_kbit=self.floor, max_kbit=self.ceiling)
            act = self.private/f'actuator-{host}.json'
            sock = self.sockets/host
            save(act, dict(cluster='cooperative-lab', host=host, owner='fabric-test',
                           agent_uid=UID, agent_gid=GID, **local))
            self.actuators[host] = self.lab.spawn(index, [sys.executable, '-B', ROOT/'scripts/fabric_actuator.py',
                act, '--socket', sock], f'actuator-{host}', privileged=True)
            until(lambda: self.data_cap(index) == self.floor, 'independent baseline readback')
            if self.mode in ('fabric', 'cooperative'):
                user_dir(self.private/f'{host}-state')
                cfg = dict(cluster='cooperative-lab', host=host, coordinator=endpoint(f'192.0.2.{4*index+1}'),
                    tls=self.identities[host], interfaces=['data0','ctrl0'], services=[SERVICES[host]], poll_ms=450,
                    egress=dict(local, socket=str(sock)))
                if self.mode == 'cooperative' and host != 'c':
                    service = SERVICES[host]
                    cfg['cooperative'] = dict(summary_path=str(self.summaries[service]), service=service,
                                              advice_path=str(self.summaries[service].with_name('cap.json')))
                save_user(self.private/f'agent-{host}.json', cfg)
        if self.mode == 'independent':
            return [self.data_cap(i) for i in range(3)]
        self.coordinator_start()
        for index, host in enumerate(HOSTS):
            self.agents[host] = self.agent_start(index, host)
        until(self.verify, 'verified enrollment')
        return self.reconcile()

    def reconcile(self):
        started = time.monotonic_ns()
        first = self.cli('reconcile', '--apply')
        until(self.verify, 'verified cap')
        second = None
        if self.cli('status')['pending_increases']:
            second = self.cli('reconcile', '--apply')
            until(self.verify, 'verified increase')
        return dict(first=first, second=second, elapsed_ms=(time.monotonic_ns()-started)/1e6,
                    caps=[self.data_cap(i) for i in range(3)])

    def target(self, role):
        return dict(host=ADDR[role], port=PORTS[role], server_name=f'{role}.test', service=role)

    def app(self, index, command, label, config):
        path = self.private/f'{label}.json'
        save_user(path, config)
        return self.lab.spawn(index, [sys.executable, '-B', APP, command, path], label)

    def start_services(self):
        allowed = dict(gateway=('client',), backend=('gateway',), database=('backend','gateway'))
        children = dict(gateway=('backend','database'), backend=('database',), database=())
        for role in reversed(ROLES):
            self.app_processes[role] = self.app(INDEX[role], 'serve', role, dict(role=role, host=ADDR[role], port=PORTS[role],
                cooperative=self.mode=='cooperative', tls=self.identities[role],
                summary=str(self.summaries[role]), peers={peer:self.target(peer) for peer in children[role]},
                pins={self.pins[name]:name for name in allowed[role]}, **({'advice': str(self.summaries[role].with_name('cap.json'))}
                    if self.mode=='cooperative' and role!='database' else {})))
            self.ready(role)
        self.app(SERVER, 'bulk_sink', 'bulk-sink', dict(host=ADDR['database'], port=19003,
                 output=str(self.lab.output/'bulk.jsonl')))
        self.ready('bulk-sink')

    def ready(self, label):
        until(lambda: (self.lab.output/f'{label}.stdout').read_text().strip()=='ready', label)

    def start_load(self, label, start, duration, rate, budget, payload):
        return self.app(0, 'load', label, dict(gateway=self.target('gateway'), tls=self.identities['client'],
            cooperative=self.mode=='cooperative', seed=self.seed, start_ns=start, duration=duration,
            rate=rate, deadline_us=budget, payload_bytes=payload, output=str(self.lab.output/f'{label}.jsonl')))

    def workload(self, duration, rate, budget, payload):
        self.start_services()
        first_usage = self.overhead()
        planned = time.monotonic_ns()+1_000_000_000
        bulk = self.app(2, 'bulk_send', 'bulk-send', dict(host=ADDR['database'], port=19003,
                        start_ns=planned, duration=duration))
        client = self.start_load('requests', planned, duration, rate, budget, payload)
        first = self.counters()
        rollout = None
        if self.coordinator:
            time.sleep(max(0,(planned+2_500_000_000-time.monotonic_ns())/1e9))
            save(self.lab.output/'live-agents.json', self.cli('diagnostics'))
            rollout = self.reconcile()
        for proc in (client, bulk):
            if proc.wait(timeout=duration+8):
                raise RuntimeError('application workload failed')
        last = self.counters()
        end_usage = self.overhead()
        cpu = {name: round(row['cpu_seconds']-first_usage['processes'][name]['cpu_seconds'],3)
               for name,row in end_usage['processes'].items()}
        control = {host:sum(row[key]-first_usage['control'][host][key] for key in ('tx','rx'))
                   for host,row in end_usage['control'].items()}
        rows = [json.loads(line) for line in (self.lab.output/'requests.jsonl').read_text().splitlines()]
        if len(rows) != duration*rate:
            raise RuntimeError('incorrect offered workload')
        good = [row for row in rows if row['status']=='ok']
        end = planned+duration*1_000_000_000
        bulk_rows = [json.loads(line) for line in (self.lab.output/'bulk.jsonl').read_text().splitlines()]
        bulk_start = max((row['bytes'] for row in bulk_rows if row['time_ns'] <= planned), default=0)
        bulk_end = max((row['bytes'] for row in bulk_rows if row['time_ns'] <= end), default=0)
        if bulk_end <= bulk_start or last['shared']['qdisc']['bytes'] <= first['shared']['qdisc']['bytes']:
            raise RuntimeError('no confirmed bulk or shared bottleneck traffic')
        counts = Counter(row['status'] for row in rows)
        latencies = [row['elapsed_us']/1000 for row in good]
        return dict(offered=len(rows), successful=len(good), outcomes=dict(counts),
            successful_p99_ms=percentile(latencies,.99), deadline_attainment=sum(row['met'] for row in rows)/len(rows),
            error_rate=(len(rows)-len(good)-counts['deferred'])/len(rows),
            deferred_rate=counts['deferred']/len(rows),
            decomposition=decompose(rows),
            goodput_requests_s=sum(row['scheduled_ns']+row['elapsed_us']*1000 <= end for row in good)/duration,
            bulk_mbps=(bulk_end-bulk_start)*8/duration/1e6, rollout=rollout,
            control_cpu_seconds=cpu, control_link_bytes=control,
            before=first, after=last)

    def partition(self):
        self.lab.command(1, ['ip','link','set','ctrl0','down'])
        start = time.monotonic()
        try:
            until(lambda: self.data_cap(1)==self.floor, 'watchdog fallback')
            fallback = time.monotonic()-start
            until(lambda: self.cli('status')['health']['b']=='stale', 'partition detected')
            blocked = self.cli('reconcile', '--apply')
            if blocked['state'] != 'waiting_for_verified':
                raise RuntimeError('partition permitted a new rollout')
            proc = self.start_load('partition-probe', time.monotonic_ns()+300_000_000, 1, 5, 1_000_000, 512)
            if proc.wait(timeout=8):
                raise RuntimeError('partition probe infrastructure failed')
            successes = sum(json.loads(row)['status']=='ok' for row in
                            (self.lab.output/'partition-probe.jsonl').read_text().splitlines())
        finally:
            self.lab.command(1, ['ip','link','set','ctrl0','up'])
        start = time.monotonic()
        until(self.verify, 'partition recovered')
        return dict(fallback_seconds=fallback, recovery_seconds=time.monotonic()-start, probe_success=successes,
                    probe_offered=5, rollout_blocked=True)


    def probe(self, label):
        proc = self.start_load(label, time.monotonic_ns()+300_000_000, 1, 5, 1_000_000, 512)
        if proc.wait(timeout=8):
            raise RuntimeError(f'{label} infrastructure failed')
        return dict(Counter(json.loads(row)['status'] for row in
                   (self.lab.output/f'{label}.jsonl').read_text().splitlines()))

    def restart_faults(self):
        stop(self.app_processes['backend'], abrupt=True)
        during_service_outage = self.probe('backend-outage')
        if during_service_outage.get('partial') != 5:
            raise RuntimeError('backend outage was not visible as a partial result')
        now = time.monotonic()
        self.app_processes['backend'] = self.lab.spawn(1, [sys.executable, '-B', APP, 'serve',
            self.private/'backend.json'], 'backend-restart')
        self.ready('backend-restart')
        after_service_restart = self.probe('backend-restored')
        service_recovery_seconds = time.monotonic()-now
        if after_service_restart.get('ok') != 5:
            raise RuntimeError('application service did not recover')

        previous = self.cli('status')
        now = time.monotonic()
        stop(self.coordinator, abrupt=True)
        until(lambda: all(self.data_cap(index)==self.floor for index in range(3)), 'coordinator crash fallback')
        fallback_seconds = time.monotonic()-now
        during_coordinator_outage = self.probe('coordinator-outage')
        now = time.monotonic()
        self.coordinator_start()
        until(self.verify, 'coordinator restarted with verified caps')
        current = self.cli('status')
        if any(previous[key] != current[key] for key in ('epoch','generation')):
            raise RuntimeError('coordinator lineage changed during restart')
        return dict(during_service_outage=during_service_outage, after_service_restart=after_service_restart,
            service_recovery_with_probe_seconds=service_recovery_seconds,
            during_coordinator_outage=during_coordinator_outage, coordinator_fallback_seconds=fallback_seconds,
            coordinator_recovery_seconds=time.monotonic()-now)


def decompose(rows):
    groups, links = {}, {}
    def collect(mapping, name, **measurements):
        values = mapping.setdefault(name, {key: [] for key in measurements})
        for key, value in measurements.items():
            if value is not None:
                values[key].append(value)
    for row in rows:
        spans = {node['span']: node for node in row['spans']}
        hops = [('client', row['hop'])] if 'hop' in row else []
        for node in spans.values():
            collect(groups, node['service'], local_duration_us=node['duration_us'],
                    processing_interval_us=node['processing_us'])
            hops.extend((node['service'], hop) for hop in node['hops'])
        for service, hop in hops:
            child = spans.get(hop['child'])
            elapsed = hop['elapsed_us']
            residual = elapsed-child['duration_us'] if child and child['duration_us'] <= elapsed else None
            collect(links, f"{service}-{hop['service']}", caller_elapsed_us=elapsed,
                    remote_reported_duration_us=child['duration_us'] if child else None,
                    unmatched_residual_us=residual, tcp_srtt_us=hop['srtt_us'])
    def summaries(mapping):
        return {name: {key: dict(count=len(values), p95=percentile(values,.95)) for key,values in row.items()}
                for name,row in mapping.items()}
    return dict(services=summaries(groups), hops=summaries(links))


def trial(directory, mode, seed, args, test_partition):
    owned_directory(directory)
    lab = Lab(directory)
    with tempfile.TemporaryDirectory(prefix='cs-', dir=ROOT/'results') as name:
        private = Path(name)
        private.chmod(0o755)
        try:
            topology = setup(lab)
            experiment = Trial(lab, private, mode, seed)
            initial = experiment.setup_control()
            work = experiment.workload(args.duration,args.rate,args.deadline_ms*1000,args.payload)
            result = dict(mode=mode, seed=seed, shared_htb=topology, initial=initial,
                          workload=work, partition=None)
            save(directory/'run.json', result)
            if test_partition and mode in ('cooperative','fabric'):
                result['partition'] = experiment.partition()
                save(directory/'run.json', result)
                if args.extended_recovery and mode=='cooperative':
                    result['restart_faults'] = experiment.restart_faults()
                    save(directory/'run.json', result)
            return result
        finally:
            lab.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--duration', type=int, default=7)
    parser.add_argument('--rate', type=int, default=32)
    parser.add_argument('--deadline-ms', type=int, default=180)
    parser.add_argument('--payload', type=int, default=4096)
    parser.add_argument('--seed', type=int, default=917202)
    parser.add_argument('--mode', choices=(*MODES,'all'), default='all')
    parser.add_argument('--skip-partition', action='store_true')
    parser.add_argument('--extended-recovery', action='store_true')
    args = parser.parse_args()
    if (os.geteuid() != 0 or UID == 0 or not 1 <= args.repetitions <= 12 or not 5 <= args.duration <= 20 or
            not 1 <= args.rate <= 80 or not 25 <= args.deadline_ms <= 2000 or not 128 <= args.payload <= 16_384 or
            not 0 <= args.seed <= 2**32-13):
        parser.error('use sudo as an ordinary operator and bounded workload parameters')
    if args.extended_recovery and (args.mode != 'cooperative' or args.skip_partition):
        parser.error('extended recovery requires a cooperative run with partition probes enabled')
    args.output = args.output.resolve()
    if not args.output.is_relative_to(ROOT/'results') or args.output == ROOT/'results':
        parser.error('output must be a new directory under results/')
    if not (ROOT/'results').exists():
        owned_directory(ROOT/'results')
    owned_directory(args.output)
    cfg = vars(args).copy()
    del cfg['output']
    save(args.output/'environment.json', dict(config=cfg,kernel=os.uname().release,architecture=os.uname().machine,
        cpus=os.cpu_count(), revision=subprocess.check_output(['git','-C',ROOT,'rev-parse','HEAD'],text=True).strip(),
        dirty=bool(subprocess.check_output(['git','-C',ROOT,'status','--porcelain'],text=True))))
    rng = random.Random(args.seed)
    order = []
    for block in range(args.repetitions):
        modes = list(MODES) if args.mode == 'all' else [args.mode]
        rng.shuffle(modes)
        order.extend(dict(mode=mode, block=block, seed=args.seed+block) for mode in modes)
    save(args.output/'order.json', order)
    runs = []
    for row in order:
        label = f"{row['mode']}-{row['block']:02}"
        try:
            value = trial(args.output/label, row['mode'], row['seed'], args,
                          row['block']==0 and not args.skip_partition)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            value = dict(mode=row['mode'], seed=row['seed'], error=str(error))
        runs.append(dict(block=row['block'], **value))
        save(args.output/'results.json', runs)
        print(label, value.get('error',value.get('workload',{}).get('successful_p99_ms')), flush=True)
    return 1 if any('error' in run for run in runs) else 0


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    sys.exit(main())
