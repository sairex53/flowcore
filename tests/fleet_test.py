"""Fleet policy admission and rollout invariants without privileged networking."""
import base64
import copy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from fabric_test import cluster
from fabric_agent import Agent
from fabric_coordinator import Coordinator
from fabric_protocol import Rejected
from fleet_policy import DOMAIN, load, schema


def policy():
    return dict(version=1, policy='api-cap', revision=1, owner='network', cluster='cluster',
        host_groups={'nodes': ['a', 'b', 'c']}, service_groups={'apis': ['api']},
        target=dict(hosts='nodes', services='apis', classes=['critical']),
        caps_kbit=dict(a=250, b=250, c=250),
        controller=dict(mode='fixed-cap', on_loss='protected-floor'),
        slo=dict(max_p99_us=100000, max_p99_increase_us=30000, max_error_ppm=10000,
                 max_deadline_miss_ppm=10000, min_samples=10, min_egress_kbit=0,
                 min_jain_ppm=0, max_controller_actions=0),
        stages=dict(canary=['a'], percentages=[50], verify_seconds=5,
                    observe_seconds=12, confirm_windows=2))


class RolloutTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'desired.json'
        self.cfg = cluster()
        self.cfg['admins'].append('e'*64)
        self.cfg['fleet'] = dict(
            repositories={'git': dict(path=temporary.name, bundle='fleet.json', require_signature=False, keys={})},
            operators={'d'*64: dict(name='alice', owners=['network']),
                       'e'*64: dict(name='bob', owners=['network'])},
            owners={'network': dict(repositories=['git'], hosts=list('abc'),
                                    services=['api'], classes=['critical'])})
        self.source = dict(repository='git', commit='f'*40, author='Fixture <fixture@example.invalid>',
                           signer=None, key_fingerprint=None, bundle_sha256='0'*64)
        self.now = 100
        self.start()
        self.bundle = policy()
        self.sync()

    def start(self):
        self.c = Coordinator(self.cfg, self.path, clock=lambda: self.now)
        self.c.fleet.wall = lambda: round(self.now*1e9)
        self.sessions = {}
        self.seq = {}

    @property
    def phase(self):
        return self.c.fleet.data['active']['phase']

    def sync(self, skip=(), fail=None, p99=10000, tick=True):
        for host in 'abc':
            if host in skip:
                continue
            common = dict(version=1, cluster='cluster', host=host)
            if host not in self.sessions:
                self.sessions[host] = self.c.dispatch(host*64, dict(common, op='hello',
                    boot_id='1'*32, capabilities=['telemetry_v1', 'cap_v1', 'fleet_metrics_v1']))['session']
                self.seq[host] = 0
            self.seq[host] += 1
            state = self.c.state
            ack = dict(epoch=state['epoch'], generation=state['generation'], resource='link',
                       rate_kbit=state['quotas'][host], verified=host != fail,
                       state='error' if host == fail else 'applied')
            telemetry = dict(age_ms=0, sample_end_ns=round(self.now*1e9), interfaces=[],
                             services=[dict(name='api', p99_us=p99, samples=25, errors=0,
                                            misses=0, window_seconds=1)],
                             daemon_healthy=True, controller='DISABLED', decision=None,
                             bottleneck=None, egress_kbit=90, controller_actions=0,
                             controller_session='daemon')
            self.c.dispatch(host*64, dict(common, op='heartbeat', seq=self.seq[host],
                session=self.sessions[host], telemetry=telemetry, ack=ack))
        if tick:
            self.c.tick()

    def prepare(self, mode='full', pin='d'*64, percent=None):
        args = (schema(self.bundle), self.source, mode, percent)
        plan = self.c.fleet.update(pin, 'plan', {}, args)
        return args, plan['approval']

    def deploy(self, mode='full', pin='d'*64, percent=None):
        args, approve = self.prepare(mode, pin, percent)
        return self.c.fleet.update(pin, 'rollout', {'approve': approve}, args)

    def drive(self, count=20, p99=10000):
        for _ in range(count):
            self.now += 1
            self.sync(p99=p99)

    def test_partial_promote_and_emergency_rollback(self):
        self.deploy('canary')
        self.drive(5)
        self.assertEqual(self.phase, 'paused')
        self.assertEqual(self.c.state['quotas'], dict(a=250, b=100, c=100))
        self.deploy('percentage', percent=50)
        self.drive(5)
        self.assertEqual(self.phase, 'paused')
        self.assertEqual(sum(rate == 250 for rate in self.c.state['quotas'].values()), 2)
        self.deploy()
        self.drive(5)
        self.assertEqual(self.phase, 'complete')
        active = self.c.fleet.data['active']
        self.c.dispatch('d'*64, dict(op='fleet_rollback', version=1,
            generation=self.c.state['generation'], approval=active['approval']))
        self.assertEqual(self.phase, 'rollback_applying')
        self.drive(2)
        self.assertEqual(self.phase, 'rolled_back')
        self.assertEqual(self.c.state['quotas'], dict(a=100, b=100, c=100))
        with self.assertRaisesRegex(Rejected, 'stale_policy_revision'):
            self.prepare()

    def test_shadow_records_current_evidence_without_consuming_revision(self):
        self.bundle['slo']['max_p99_us'] = 1
        before = self.c.state['generation']
        shadow = self.deploy('shadow')
        self.assertEqual(shadow['state'], 'shadow')
        self.assertEqual(shadow['evidence']['violation_count'], 3)
        self.assertEqual(self.c.state['generation'], before)
        self.assertEqual(self.c.fleet.data['highwater'], {})
        self.assertIsNone(self.c.fleet.data['active'])

    def test_preflight_rejects_infeasible_stage(self):
        self.c.persist(dict(self.c.state, quotas=dict(a=800, b=100, c=100)))
        self.sync()
        self.bundle['caps_kbit'] = dict(a=100, b=800, c=100)
        self.bundle['stages']['canary'] = ['b']
        with self.assertRaisesRegex(Rejected, 'infeasible_intermediate_stage'):
            self.prepare()
        self.bundle['controller']['mode'] = 'unrestricted'
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            schema(self.bundle)

    def test_collateral_service_outside_owner_scope_rejected(self):
        self.cfg['agents']['a']['services']['backup'] = 'background'
        with self.assertRaisesRegex(Rejected, 'collateral_outside_owner_scope'):
            self.prepare()

    def test_concurrent_approval_conflict(self):
        a, first = self.prepare('canary')
        b, second = self.prepare('canary', pin='e'*64)
        self.assertNotEqual(first, second)
        self.c.fleet.update('d'*64, 'rollout', {'approve': first}, a)
        with self.assertRaisesRegex(Rejected, 'stale_policy_revision|approval_mismatch'):
            self.c.fleet.update('e'*64, 'rollout', {'approve': second}, b)
        with self.assertRaisesRegex(Rejected, 'fleet_owns_rollouts'):
            self.c.dispatch('d'*64, dict(version=1, op='reconcile', apply=True))

    def test_kernel_failure_rollback_stays_unverified_until_recovery(self):
        self.deploy()
        self.now += 1
        self.sync(fail='a')
        self.assertEqual(self.phase, 'rollback_applying')
        self.now += 7
        self.sync(skip='a')
        self.assertEqual(self.phase, 'rollback_blocked')
        self.assertNotIn('rollback_verified', [row['event'] for row in self.c.fleet.data['history']])
        self.sync()
        self.assertEqual(self.phase, 'rolled_back')

    def test_partition_blocks_restoring_increases(self):
        self.c.persist(dict(self.c.state, quotas=dict(a=300, b=300, c=100)))
        self.sync()
        self.bundle['caps_kbit'] = dict(a=100, b=300, c=100)
        self.deploy('canary')
        self.sync()
        self.assertEqual(self.phase, 'observing')
        self.now += 7
        self.sync(skip='a')
        self.assertEqual(self.phase, 'rollback_lowering')
        self.assertEqual(self.c.state['quotas']['a'], 100)
        self.now += 7
        self.sync(skip='a')
        self.assertEqual(self.phase, 'rollback_blocked')
        self.assertEqual(self.c.state['quotas']['a'], 100)
        self.sync()
        self.assertEqual(self.phase, 'rollback_applying')
        self.assertEqual(self.c.state['quotas']['a'], 300)
        self.sync()
        self.assertEqual(self.phase, 'rolled_back')

    def test_restart_holds_rollout_until_fresh_ack(self):
        self.deploy('canary')
        generation = self.c.state['generation']
        self.now += 1
        self.start()
        self.c.tick()
        self.assertEqual(self.phase, 'applying')
        self.assertEqual(self.c.state['generation'], generation)
        self.sync(skip='b')
        self.assertEqual(self.phase, 'applying')
        self.sync()
        self.drive(5)
        self.assertEqual(self.phase, 'paused')

    def test_guard_requires_distinct_confirmed_windows(self):
        self.deploy('canary')
        self.sync()
        self.sync(p99=140000)
        self.now += 1
        self.sync(p99=140000)
        self.sync(p99=140000)
        self.assertEqual(self.phase, 'observing')
        self.now += 1
        self.sync(p99=140000)
        self.assertEqual(self.phase, 'rollback_applying')
        self.assertEqual(self.c.fleet.data['active']['reason'], 'guardrail_violation')


class CompatibilityTest(unittest.TestCase):
    def test_new_agent_can_report_to_legacy_coordinator(self):
        agent = Agent.__new__(Agent)
        agent.session, agent.state, agent.local, agent.boot = None, None, None, '0'*32
        agent.capabilities = ['telemetry_v1', 'fleet_metrics_v1']
        agent.collector = SimpleNamespace(sample=lambda: dict(age_ms=0, sample_end_ns=5,
            controller_actions=0, controller_session='local'))
        seen = []

        def call(op, **fields):
            seen.append((op, fields))
            if op == 'hello':
                if 'fleet_metrics_v1' in fields['capabilities']:
                    raise Rejected('capability_mismatch')
                return dict(session='0'*48, epoch='1'*32)
            self.assertEqual(fields['telemetry'], {'age_ms': 0})
            return {'desired': None}

        agent.call = call
        agent.tick()
        self.assertEqual([item[0] for item in seen], ['hello', 'hello', 'heartbeat'])


class GitTest(unittest.TestCase):
    def test_signed_immutable_bundle_and_tampering(self):
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError:
            self.skipTest('optional cryptography package unavailable')
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args],
                    text=True, stderr=subprocess.PIPE).strip()
            git('init', '-q')
            git('config', 'user.name', 'Test signer')
            git('config', 'user.email', 'test@example.invalid')
            private = Ed25519PrivateKey.generate()
            public = base64.b64encode(private.public_key().public_bytes_raw()).decode()
            bundle = json.dumps(policy()).encode()
            signature = base64.b64encode(private.sign(DOMAIN+bundle)).decode()
            (repo/'fleet.json').write_bytes(bundle)
            (repo/'fleet.json.sig').write_text(json.dumps(dict(key_id='a', signature=signature)))
            git('add', '.')
            git('commit', '-qm', 'first')
            original = git('rev-parse', 'HEAD')
            repo_config = dict(path=str(repo), bundle='fleet.json', require_signature=True, keys={'a': public})
            parsed, source = load(repo_config, original)
            self.assertEqual(parsed, policy())
            self.assertEqual(source['signer'], 'a')
            self.assertIn('Test signer', source['author'])
            altered = copy.deepcopy(parsed)
            altered['revision'] += 1
            (repo/'fleet.json').write_text(json.dumps(altered))
            git('add', '.')
            git('commit', '-qm', 'tamper')
            with self.assertRaisesRegex(Rejected, 'invalid_signature'):
                load(repo_config, git('rev-parse', 'HEAD'))
            self.assertEqual(load(repo_config, original)[0], policy())
            git('rm', 'fleet.json.sig')
            git('commit', '-qm', 'unsigned')
            unsigned = git('rev-parse', 'HEAD')
            with self.assertRaisesRegex(Rejected, 'signature_required'):
                load(repo_config, unsigned)
            self.assertIsNone(load(dict(repo_config, require_signature=False), unsigned)[1]['signer'])
            with self.assertRaisesRegex(Rejected, 'full_git_commit_required'):
                load(repo_config, 'HEAD')


if __name__ == '__main__':
    unittest.main()
