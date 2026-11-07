"""Deterministic Fabric allocation, replay, authorization and rollout checks."""
from pathlib import Path
import random
import socket
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from fabric_actuator import Actuator
from fabric_coordinator import Coordinator, allocate
from fabric_protocol import LIMIT, Rejected, receive


def rule(host, target):
    return dict(id=host, match=dict(hosts=[host], services=['api'], groups=['blue'],
                     environment='test', **{'class': 'critical'}),
                precedence=50, weight=1, desired_kbit=target, target_p99_us=50000)


def cluster():
    def row(pin):
        return dict(pins=[pin*64], environment='test', groups=['blue'], services={'api': 'critical'},
                    egress=dict(resource='link', interface='data0', floor_kbit=100, max_kbit=900))
    return dict(cluster='cluster', listen=dict(host='127.0.0.1', port=6000), tls={},
                admins=['d'*64], agents=dict(a=row('a'), b=row('b'), c=row('c')),
                resources={'link': dict(capacity_kbit=1000, path=['test-egress'])},
                intents=[rule('a', 800), rule('b', 100), rule('c', 100)])


class FabricTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 10
        self.path = Path(self.temp.name) / 'desired.json'
        self.cfg = cluster()
        self.controller = Coordinator(self.cfg, self.path, lambda: self.now)

    def request(self, host, operation, **fields):
        message = dict(version=1, op=operation, **fields)
        if host != 'd':
            message.update(host=host, cluster='cluster')
        return self.controller.dispatch(host*64, message)

    def enroll(self, host):
        return self.request(host, 'hello', boot_id='1'*32, capabilities=['telemetry_v1', 'cap_v1'])['session']

    def report(self, host, session, sequence=1, acknowledge=True):
        state = self.controller.state
        ack = (dict(epoch=state['epoch'], generation=state['generation'], resource='link',
                    rate_kbit=state['quotas'][host], verified=True, state='applied')
               if acknowledge else None)
        data = dict(age_ms=0, interfaces=[], services=[], daemon_healthy=None,
                    controller=None, decision=None, bottleneck=None, egress_kbit=None)
        return self.request(host, 'heartbeat', session=session, seq=sequence, telemetry=data, ack=ack)

    def test_lower_before_increases_and_recover_pending_state(self):
        sessions = {host: self.enroll(host) for host in 'abc'}
        for host, session in sessions.items():
            self.report(host, session)
        self.request('d', 'reconcile', apply=True)
        self.assertEqual(self.controller.state['quotas'], {'a': 800, 'b': 100, 'c': 100})
        self.cfg['intents'] = [rule('a', 100), rule('b', 800), rule('c', 100)]
        for host, session in sessions.items():
            self.report(host, session, 2)
        self.request('d', 'reconcile', apply=True)
        self.assertEqual(self.controller.state['quotas'], {'a': 100, 'b': 100, 'c': 100})
        self.assertEqual(self.controller.state['pending'], {'a': 100, 'b': 800, 'c': 100})
        self.controller = Coordinator(self.cfg, self.path, lambda: self.now)
        self.assertEqual(self.request('d', 'reconcile', apply=True)['waiting'], list('abc'))
        for host in 'abc':
            self.report(host, self.enroll(host), acknowledge=host != 'a')
        self.assertEqual(self.request('d', 'reconcile', apply=True)['waiting'], ['a'])
        self.report('a', self.enroll('a'))
        self.assertEqual(self.request('d', 'reconcile', apply=True)['state'], 'increases_committed')
        self.assertEqual(self.controller.state['quotas'], {'a': 100, 'b': 800, 'c': 100})

    def test_replay_does_not_refresh_liveness(self):
        session = self.enroll('a')
        self.report('a', session)
        self.now += 5
        with self.assertRaisesRegex(Rejected, 'replay'):
            self.report('a', session)
        self.assertEqual(self.request('d', 'agents')['a']['health'], 'stale')
        self.enroll('a')
        with self.assertRaisesRegex(Rejected, 'unknown_session'):
            self.report('a', session, 2)

    def test_scope_and_config_protection(self):
        with self.assertRaisesRegex(Rejected, 'unauthorized'):
            self.controller.dispatch('a'*64, dict(version=1, op='hello', host='b', cluster='cluster'))
        with self.assertRaisesRegex(Rejected, 'agent_operation'):
            self.request('a', 'reconcile')
        self.cfg['agents']['a']['egress']['max_kbit'] = 700
        with self.assertRaisesRegex(ValueError, 'scope changed'):
            Coordinator(self.cfg, self.path)

    def test_rotation_overlap_preserves_lineage(self):
        previous = self.controller.state
        self.cfg['agents']['a']['pins'].append('e'*64)
        self.controller = Coordinator(self.cfg, self.path)
        for pin in ('a'*64, 'e'*64):
            self.controller.dispatch(pin, dict(version=1, op='hello', cluster='cluster',
                                     host='a', boot_id='1'*32, capabilities=['telemetry_v1']))
        self.assertEqual(self.controller.state, previous)
        self.cfg['agents']['a']['pins'] = ['e'*64]
        self.controller = Coordinator(self.cfg, self.path)
        with self.assertRaisesRegex(Rejected, 'unauthorized'):
            self.enroll('a')

    def test_local_generation_conflict_and_lease_expiry(self):
        cfg = dict(cluster='cluster', host='a', interface='data0', resource='link',
                   floor_kbit=100, max_kbit=500)
        actuator = Actuator(cfg, None, lambda: self.now)
        actuator.apply_rate = lambda rate: setattr(actuator, 'rate', rate)
        request = {key: cfg[key] for key in ('cluster', 'host', 'interface', 'resource')}
        request.update(epoch='a'*32, generation=4, rate_kbit=250, lease_ms=3000)
        self.assertEqual(actuator.apply(request)['rate_kbit'], 250)
        with self.assertRaisesRegex(Rejected, 'generation_conflict'):
            actuator.apply(dict(request, rate_kbit=300))
        with self.assertRaisesRegex(Rejected, 'stale_generation'):
            actuator.apply(dict(request, generation=3))
        self.now += 3
        actuator.expire()
        self.assertEqual((actuator.state, actuator.rate), ('fallback', 100))

    def test_allocator_bounds_and_determinism(self):
        rng = random.Random(731)
        for _ in range(150):
            bounds = {}
            for i in range(8):
                floor = rng.randint(1, 100)
                bounds[str(i)] = (floor, floor + rng.randint(0, 900), rng.randint(1, 16))
            capacity = sum(row[0] for row in bounds.values()) + rng.randint(0, 2000)
            result = allocate(capacity, bounds)
            self.assertEqual(result, allocate(capacity, dict(reversed(list(bounds.items())))))
            self.assertEqual(sum(result.values()), min(capacity, sum(row[1] for row in bounds.values())))
            self.assertTrue(all(bounds[host][0] <= rate <= bounds[host][1] for host, rate in result.items()))

    def test_oversized_frame_rejected_before_body(self):
        sender, receiver = socket.socketpair()
        with sender, receiver:
            sender.sendall(struct.pack('!I', LIMIT+1))
            with self.assertRaisesRegex(Rejected, 'frame_size'):
                receive(receiver)


if __name__ == '__main__':
    unittest.main()
