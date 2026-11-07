#!/usr/bin/env python3
"""Signed policy rollout fixture using Fabric TLS, SDK metrics, and real HTB."""
import argparse
import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from benchmark import GID, UID, Lab, owned_directory
from fabric_lab import APPS, BINDIR, HOSTS, ROOT, ROUTER, SERVER, Experiment, save, save_user, setup, stop, until, user_dir
from fleet_policy import DOMAIN


def bundle(revision, rates, *, p99=500000, windows=2):
    return dict(version=1, policy='fixture-egress', owner='network', revision=revision, cluster='fabric-lab',
                host_groups={'senders': list(HOSTS)}, service_groups={'http': ['api', 'batch']},
                target=dict(hosts='senders', services='http', classes=['critical', 'standard']),
                caps_kbit=dict(zip(HOSTS, rates)), controller=dict(mode='fixed-cap', on_loss='protected-floor'),
                slo=dict(max_p99_us=p99, max_p99_increase_us=None, max_error_ppm=100000,
                         max_deadline_miss_ppm=100000, min_samples=6, min_egress_kbit=0,
                         min_jain_ppm=0, max_controller_actions=0),
                stages=dict(canary=['a'], percentages=[50], verify_seconds=6,
                            observe_seconds=18, confirm_windows=windows))


class Fixture(Experiment):
    def __init__(self, lab, private):
        super().__init__(lab, private, 'fabric', 7107)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        self.signer = Ed25519PrivateKey.generate()
        public = base64.b64encode(self.signer.public_key().public_bytes_raw()).decode()
        self.repo = user_dir(private/'policy-repo')
        self.git('init', '-q')
        self.git('config', 'user.name', 'Fleet Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.fleet_config = dict(
            repositories={'fixture': dict(path=str(self.repo), bundle='fleet.json',
                                          require_signature=True, keys={'fixture-key': public})},
            operators={self.pins['operator']: dict(name='fixture-operator', owners=['network'])},
            owners={'network': dict(repositories=['fixture'], hosts=list(HOSTS),
                                    services=['api', 'batch'], classes=['critical', 'standard'])})

    def git(self, *args):
        return subprocess.run(['setpriv', f'--reuid={UID}', f'--regid={GID}', '--clear-groups',
                               'git', '-C', str(self.repo), *args],
                              capture_output=True, text=True, check=True, timeout=5).stdout.strip()

    def repository_commit(self, policy):
        path = self.repo/'fleet.json'
        save_user(path, policy)
        signature = base64.b64encode(self.signer.sign(DOMAIN+path.read_bytes())).decode()
        save_user(self.repo/'fleet.json.sig', dict(key_id='fixture-key', signature=signature))
        self.git('add', 'fleet.json', 'fleet.json.sig')
        self.git('commit', '-qm', f"Fixture policy revision {policy['revision']}")
        return self.git('rev-parse', 'HEAD')

    def fleet(self, action, **options):
        args = [str(BINDIR/'flowcore'), 'fleet', action, '--config', str(self.operator_config), '--json']
        for key, value in options.items():
            args.extend(['--'+key.replace('_', '-'), str(value)])
        return json.loads(self.lab.command(ROUTER, args))

    def deploy(self, commit, mode, **options):
        args = dict(repository='fixture', commit=commit, mode=mode, **options)
        plan = self.fleet('plan', **args)
        assert not plan['validation']['waiting'], plan['validation']
        assert plan['plan']['source']['signer'] == 'fixture-key'
        return self.fleet('rollout', approve=plan['approval'], **args)

    def stage(self, expected, timeout=25):
        def check():
            status = self.fleet('status')
            if status['active'] and status['active']['phase'] == expected:
                return status
            return None
        return until(check, expected, timeout)

    def caps(self):
        return [self.data_cap(i) for i in range(3)]

    def workloads(self):
        for index, port in enumerate((19000, 19100)):
            self.lab.spawn(SERVER, [sys.executable, '-B', APPS/'http_service.py',
                '--bind', '198.18.4.2', '--critical-port', port, '--background-port', port+1,
                '--database-port', port+2], f'server-{index}')
        time.sleep(.3)
        planned = time.monotonic_ns()+2_000_000_000
        processes = []
        for index, host in enumerate(HOSTS):
            service, priority = ('api', 'critical') if host == 'a' else ('batch', 'standard')
            processes.append(self.lab.spawn(index, ['env',
                f'FLOWCORE_SOCKET={self.private}/d-{host}/control.sock',
                sys.executable, '-B', APPS/'http_benchmark.py', '--host', '198.18.4.2',
                '--port', 19000 if host == 'a' else 19100, '--duration', 60, '--rate', 40,
                '--deadline-ms', 200, '--concurrency', 4, '--service', service,
                '--priority', priority, '--start-ns', planned, '--background-bytes', 0,
                '--output', self.lab.output/f'{host}.json'], f'client-{host}'))
        until(lambda: all(row['expected_hosts'] == row['sampled_hosts'] for row in
                  self.cli('services')['aggregate'].values()), 'application telemetry', 15)
        return processes

    def rollout(self):
        initial = self.start(coordinate=False)
        assert initial['caps'] == [500, 500, 500]
        clients = self.workloads()
        first = self.repository_commit(bundle(1, [1200, 900, 900]))
        validation = self.fleet('validate', repository='fixture', commit=first)
        dry = self.fleet('rollout', repository='fixture', commit=first, mode='dry-run')
        assert validation['approval_state'] == dry['approval_state'] == 'unapproved'
        assert self.deploy(first, 'shadow')['state'] == 'shadow'
        assert self.caps() == [500, 500, 500]
        canary_start = time.monotonic()
        first_status = self.deploy(first, 'canary')
        assert first_status['active']['phase'] in ('applying', 'observing')
        self.stage('observing')
        crashed = time.monotonic()
        stop(self.coordinator, abrupt=True)
        self.coordinator_start((4000, 1000, 1000))
        canary = self.stage('paused')
        assert self.caps() == [1200, 500, 500]
        canary_timing = dict(total_seconds=time.monotonic()-canary_start,
                            restart_to_pause_seconds=time.monotonic()-crashed,
                            generation=canary['generation'], caps=self.caps())
        percentage_start = time.monotonic()
        self.deploy(first, 'percentage', percent=50)
        partial = self.stage('paused')
        caps = self.caps()
        assert caps[0] == 1200 and sorted(caps[1:]) == [500, 900]
        percentage = dict(seconds=time.monotonic()-percentage_start, caps=caps, generation=partial['generation'])
        full_start = time.monotonic()
        self.deploy(first, 'full')
        final = self.stage('complete')
        assert self.caps() == [1200, 900, 900]
        full = dict(seconds=time.monotonic()-full_start, caps=self.caps(), generation=final['generation'])
        try:
            self.fleet('rollout', repository='fixture', commit=first, mode='dry-run')
        except subprocess.CalledProcessError as error:
            assert 'stale_policy_revision' in error.stderr
        else:
            raise AssertionError('previous policy revision was accepted')

        second = self.repository_commit(bundle(2, [500, 900, 900], windows=5))
        self.deploy(second, 'canary')
        self.stage('observing')
        loss = time.monotonic()
        self.lab.command(2, ['ip', 'link', 'set', 'ctrl0', 'down'])
        try:
            blocked = self.stage('rollback_blocked')
            assert self.caps() == [500, 900, 500]
            assert blocked['active']['reason'] == 'agent_lost'
            partition = dict(blocked_seconds=time.monotonic()-loss, caps=self.caps(),
                             waiting=blocked['waiting'], phase=blocked['active']['phase'])
            self.data_probe(2)
        finally:
            self.lab.command(2, ['ip', 'link', 'set', 'ctrl0', 'up'])
        resumed = time.monotonic()
        self.stage('rolled_back')
        assert self.caps() == [1200, 900, 900]
        partition.update(recovery_seconds=time.monotonic()-resumed, recovered_caps=self.caps(),
                         application_probe='HTTP 200')

        third = self.repository_commit(bundle(3, [1100, 900, 900], p99=1))
        guard_start = time.monotonic()
        self.deploy(third, 'canary')
        guard = self.stage('rolled_back')
        assert self.caps() == [1200, 900, 900]
        assert guard['active']['reason'] == 'guardrail_violation'
        events = []
        for offset in range(0, 32, 8):
            page = self.fleet('history', offset=offset)
            events.extend(page['events'])
            if len(events) >= page['total']:
                break
        violation = next(event for event in events if event['event'] == 'rollback_attempted' and event['revision'] == 3)
        checks = dict(initial=initial, source=validation['plan']['source'], canary=canary_timing,
                      percentage=percentage, full=full, partition=partition, guardrail=dict(
                          seconds=time.monotonic()-guard_start, reason=guard['active']['reason'],
                          evidence=violation['evidence'], caps=self.caps()), events=events)
        save(self.lab.output/'rollout.json', checks)
        for proc in clients:
            if proc.wait(timeout=65):
                raise RuntimeError('application client failed')
        for host in HOSTS:
            result = json.loads((self.lab.output/f'{host}.json').read_text())
            assert result['sdk']['connection'] == 'connected'
        return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or UID == 0:
        parser.error('run through sudo as a nonroot operator')
    args.output = args.output.resolve()
    owned_directory(args.output)
    lab = Lab(args.output)
    parent = ROOT/'results'
    if not parent.exists():
        owned_directory(parent)
    with tempfile.TemporaryDirectory(prefix='fleet-', dir=parent) as name:
        private = Path(name)
        private.chmod(0o755)
        try:
            setup(lab)
            fixture = Fixture(lab, private)
            fixture.rollout()
            print('Fleet namespace fixture completed', flush=True)
        finally:
            lab.close()


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    main()
