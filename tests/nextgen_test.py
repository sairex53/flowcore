"""Integrated mode parser and rollback regression tests."""
import copy
from contextlib import ExitStack
from pathlib import Path
import sys
import socket
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from nextgen_capabilities import mptcp_loopback
from multipath import PROTOCOL
from nextgen_scope import feedback
from pacing_workload import CEILING, PacingWorkload, SETTINGS, U64
from service_config import CLASSES, load
from research_manifest import load_manifest, validate


class Integrated(unittest.TestCase):
    def setUp(self):
        self.manifest = load_manifest(ROOT/'scenarios/nextgen-holdout.json')

    def test_excess_capacity_and_invalid_deadline(self):
        for path, value in ((('network', 'bandwidth_kbit'), 12001),
                            (('workload', 'deadline_ms'), None),
                            (('workload', 'deadline_ms'), -1),
                            (('workload', 'phases'), [None]),
                            (('controller', 'target_p99_ms'), 30001)):
            with self.subTest(path=path, value=value):
                invalid = copy.deepcopy(self.manifest)
                invalid[path[0]][path[1]] = value
                with self.assertRaises(ValueError):
                    validate(invalid)
        excessive_fault = copy.deepcopy(self.manifest)
        excessive_fault['faults'][0]['bandwidth_kbit'] = 12001
        with self.assertRaises(ValueError):
            validate(excessive_fault)

    def test_scope_refuses_foreign_filter_and_unhealthy_sample(self):
        configured = load(ROOT/'config/service.json')['policies']['default']['classes']
        tc = dict(qdisc=[dict(kind='htb', handle='1:', root=True)], filter=[])
        tc['class'] = [dict(handle='1:1', rate=1500000, ceil=1500000, **{'class':'htb'})]
        for name, minor in CLASSES.items():
            entry = configured[name]
            handle = f'1:{minor}'
            tc['qdisc'].append(dict(kind='fq_codel', parent=handle, options={'ecn':True}))
            tc['class'].append(dict(handle=handle, rate=15000*entry['min_pct'],
                                    ceil=15000*entry['max_pct'], **{'class':'htb'}))
            for protocol, eth in (('ip','ipv4'), ('ipv6','ipv6')):
                header = dict(protocol=protocol, kind='flower', parent='1:', chain=0,
                              pref=2*minor+(protocol=='ipv6'))
                tc['filter'].extend((header, dict(header, options=dict(classid=handle,
                    keys=dict(eth_type=eth, ip_proto='tcp', dst_port=entry['port'])))))

        def scope(healthy=True):
            return feedback(tc, configured, 12000, 12000, generation=1, sampled_ns=42, healthy=healthy)

        self.assertEqual(scope(), dict(sample_ns=42,generation=1,verified=True,background_guarantee=450000))
        self.assertFalse(scope(False)['verified'])
        tc['filter'][1]['options']['keys']['dst_port'] = 80
        self.assertFalse(scope()['verified'])
        tc['filter'][1]['options']['keys']['dst_port'] = configured['critical']['port']
        tc['filter'].append(dict(parent='1:', kind='bpf'))
        self.assertFalse(scope()['verified'])

    def test_loopback_mptcp_fallback_when_available(self):
        supported = mptcp_loopback(PROTOCOL)
        if not supported['connected'] or supported['negotiated'] is None:
            self.skipTest('MPTCP loopback readback unavailable')
        self.assertTrue(supported['negotiated'])
        fallback = mptcp_loopback(socket.IPPROTO_TCP)
        self.assertTrue(fallback['connected'])
        self.assertIs(fallback['negotiated'], False)

    def test_cohort_change_restores_live_tcp_caps_before_next_sample(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build', prefix='cohort-test-') as temp, ExitStack() as resources:
            listener = resources.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            listener.settimeout(1)
            listener.bind(('127.0.0.1', 0))
            listener.listen(3)
            worker = PacingWorkload(Path(temp), 'integrated')
            resources.callback(worker.finish)

            def connection():
                sender = resources.enter_context(socket.create_connection(listener.getsockname(), timeout=1))
                peer, _ = listener.accept()
                resources.enter_context(peer)
                return sender

            def cap(sock):
                values = (U64*len(SETTINGS))()
                self.assertEqual(worker.lib.fc_pacing_inspect_v2(sock.fileno(), values), 0)
                return values[1]

            first, remaining, replacement = connection(), connection(), connection()
            original_first = cap(first)

            def detach_remaining():
                for flow in tuple(worker.sockets):
                    worker.remove(flow)

            resources.callback(detach_remaining)
            worker.add(0, first)
            worker.add(1, remaining)
            self.assertTrue(worker.apply(150_000, 'adjust'))
            worker.remove(0)
            self.assertEqual(cap(first), original_first)
            self.assertEqual(cap(remaining), CEILING)
            self.assertEqual(worker.cohort_fallbacks, 1)

            self.assertTrue(worker.apply(150_000, 'adjust'))
            worker.add(0, replacement)
            self.assertEqual((cap(remaining), cap(replacement)), (CEILING, CEILING))
            self.assertEqual(worker.cohort_fallbacks, 2)
            self.assertEqual(worker.polls, 0)
            self.assertEqual(worker.cohort_revision, 4)

    def test_failed_cohort_compensation_latches_and_shuts_down_survivors(self):
        worker = PacingWorkload.__new__(PacingWorkload)
        worker.integrated = True
        worker.cohort_failed = False
        worker.cohort_revision = 0
        worker.previous = {1: 100}
        worker.previous_ecn = {}
        worker.previous_aqm = {}
        worker.rate = 150_000
        closed = []
        worker.sockets = {1: (SimpleNamespace(shutdown=closed.append), 1)}

        def refused(*_):
            raise RuntimeError('kernel refused cap restoration')

        worker.timed_apply = refused
        with tempfile.TemporaryDirectory(dir=ROOT/'build', prefix='cohort-fault-') as temp:
            worker.output = Path(temp)
            with self.assertRaisesRegex(RuntimeError, 'kernel refused'):
                worker.cohort_changed('leave', 0)
        self.assertTrue(worker.cohort_failed)
        self.assertEqual(closed, [socket.SHUT_RDWR])
        self.assertFalse(worker.previous)

    def test_group_write_refusal_compensates_prior_socket(self):
        old, wanted = 1_500_000, 200_000
        rates = {1: old, 2: old}
        confirmed = []

        def change(handle, rate):
            if handle == 2 and rate == wanted:
                return 1
            rates[handle] = rate
            return 0

        def read(fd, out):
            out[1] = rates[fd]
            return 0

        worker = PacingWorkload.__new__(PacingWorkload)
        worker.rate = old
        worker.controller = object()
        worker.lib = SimpleNamespace(fc_pacing_rate=change, fc_pacing_inspect_v2=read,
                                     fc_pacing_confirm=lambda _, result: confirmed.append(result))
        worker.sockets = {fd: (SimpleNamespace(fileno=lambda fd=fd: fd), fd) for fd in rates}
        self.assertFalse(worker.apply(wanted, 'adjust'))
        self.assertEqual(rates, {1: old, 2: old})
        self.assertEqual(confirmed, [False])
        self.assertEqual(worker.rate, old)

        def failed_compensation(handle, rate):
            if handle == 1 and rate == old:
                return 1
            return change(handle, rate)

        worker.lib.fc_pacing_rate = failed_compensation
        with self.assertRaisesRegex(RuntimeError, 'rollback could not be verified'):
            worker.apply(wanted, 'adjust')


if __name__ == '__main__':
    unittest.main()
