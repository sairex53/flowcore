"""Deterministic incident fixtures, parser bounds and request correlation."""
import copy
import json
import os
from pathlib import Path
import socket
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from causal_diagnostics import NS, WINDOW, MAX_BUNDLE, Recorder, decode_bundle, replay, signals
from daemon import Service
from service_config import load
from tcp_diag import connections


def application(p99=1000, failures=0):
    return dict(traffic_class='critical', attempts=200, successes=200-failures, failures=failures,
                deadlines=200, deadline_misses=failures, timeouts=failures, p99_us=p99, target_us=15000)


class Engine(unittest.TestCase):
    def test_causal_scope_and_replay(self):
        r = Recorder()
        r.window(WINDOW+10,WINDOW,'demo',**application())
        for stamp in (5*NS,6*NS,7*NS):
            r.record('queue',stamp,interface='fc0',direction='egress',traffic_class='critical',backlog=10000,demand=500)
            r.record('queue',stamp,interface='fc0',direction='egress',backlog=12000,drops=0,owned=True,saturated=True)
            r.record('request',stamp,'other',start_ns=stamp-20_000_000,outcome='ok',duration_us=20000,processing=19000)
        r.window(2*WINDOW+10,2*WINDOW,'demo',**application(19000,30))
        expected = r.diagnose('demo')
        bundle = r.incidents[0]
        self.assertEqual(replay(decode_bundle(json.dumps(bundle).encode())),expected)
        self.assertEqual(expected['signals'],['P99_REGRESSION','SLO_P99_EXCEEDED','FAILURES_INCREASED','DEADLINE_MISSES_INCREASED'])
        self.assertEqual(expected['candidates'][0]['name'],'QUEUE_SATURATION')
        self.assertEqual(expected['candidates'][0]['strength'],'supported')
        self.assertNotIn('APPLICATION_PROCESSING_DELAY',[c['name'] for c in expected['candidates']])
        self.assertFalse(expected['causal_effect_established'])
        r.reset_scope()
        self.assertEqual(r.diagnose('demo')['assessment'],'INSUFFICIENT_EVIDENCE')
        self.assertEqual(r.find(bundle['id']),bundle)

    def test_sparse_windows_do_not_promote_tail_or_missing_samples(self):
        self.assertEqual(signals(dict(application(19000),successes=25),application()),[])
        r = Recorder()
        r.window(WINDOW+5,WINDOW,'demo',**application())
        r.window(3*WINDOW+5,3*WINDOW,'demo',**application(18000))
        self.assertEqual(r.diagnose('demo')['signals'],['SLO_P99_EXCEEDED'])
        self.assertEqual(r.diagnose('demo')['assessment'],'INSUFFICIENT_EVIDENCE')

    def test_network_and_controller_signals(self):
        r = Recorder()
        r.window(WINDOW+5,WINDOW,'demo',**application(),throughput=200,drops=0,retransmits=0,segments=100)
        r.record('controller',5*NS,result='kernel_verified',changed=True)
        r.record('controller',6*NS,'demo',result='rolled_back',comparable=True,degraded=True)
        r.window(2*WINDOW+5,2*WINDOW,'demo',**application(6000),throughput=110,drops=7,retransmits=12,segments=100)
        report = r.diagnose('demo')
        self.assertEqual(set(report['signals']), {'P99_REGRESSION','THROUGHPUT_REDUCTION','DROP_BURST',
                                                 'RETRANSMISSION_INCREASE','POSSIBLE_CONTROLLER_REGRESSION'})
        self.assertEqual(report['candidates'][0]['strength'],'contextual')

    def test_untrusted_bundles_and_limits(self):
        r = Recorder()
        for i in range(1000):
            r.record('request',5*NS,'demo',start_ns=4*NS,outcome='ok',duration_us=1000)
        r.window(2*WINDOW+10,2*WINDOW,'demo',**application(20000))
        bundle = r.incidents[0]
        self.assertEqual(len(r.requests),128)
        self.assertLessEqual(len(bundle['events']),512)
        broken = []
        for field,value in [('schema',True),('baseline',1),('anchor',None)]:
            bad = dict(bundle,**{field:value})
            broken.append(json.dumps(bad).encode())
        bad = copy.deepcopy(bundle)
        bad['events'][0]['data']['dns'] = 2000
        broken.append(json.dumps(bad).encode())
        bad = copy.deepcopy(bundle)
        bad['events'][1]['seq'] = bad['events'][0]['seq']
        broken.append(json.dumps(bad).encode())
        for raw in broken+[b' '* (MAX_BUNDLE+1), b'{"schema":1,"schema":1}', b'{"schema":NaN}', b'['*2000+b']'*2000]:
            with self.assertRaises(ValueError):
                decode_bundle(raw)

    def test_real_socket_association_and_opt_in_stages(self):
        service = Service(load(ROOT/'config/service.json'))
        api = service.intent_api
        base = dict(op='intent_v2',version=2)
        call = lambda fd=None, **fields: api.call(dict(base,**fields),os.getuid(),os.getpid(),fd)
        lease = call(action='register',isolation='shared',intent={'name':'socketdemo','priority':'standard'})['lease']
        with socket.socket() as listen, socket.socket() as client:
            listen.bind(('127.0.0.1',0))
            listen.listen(1)
            client.connect(listen.getsockname())
            peer, _ = listen.accept()
            with peer:
                assoc = call(fd=client.fileno(),action='associate',lease=lease)
                stamp = time.monotonic_ns()
                event = dict(id=1,phase='start',start_ns=stamp,end_ns=0,cookie=int(assoc['cookie']),deadline_us=100)
                self.assertEqual(call(action='batch',lease=lease,events=[event])['accepted'],1)
                snapshot, limited = connections(128)
                matched = [dict(row,namespace='host',netns=1) for row in snapshot if row['cookie'] == f"{event['cookie']:016x}"]
                self.assertTrue(matched)
                service.observer.track(matched,False,time.monotonic_ns())
                terminal = dict(event,phase='timeout',end_ns=time.monotonic_ns(),timings_us={'processing':50,'dns':30})
                invalid = dict(terminal,timings_us={'dns':-1})
                self.assertEqual(call(action='batch',lease=lease,events=[invalid])['rejected'],1)
                self.assertEqual(call(action='batch',lease=lease,events=[terminal])['accepted'],1)
                row = service.incident.recorder.requests[-1]['data']
                self.assertEqual(row['association'],'fd_and_sample')
                self.assertEqual(row['processing'],50)
                self.assertEqual(row['outcome'],'timeout')
                self.assertNotIn(str(event['cookie']),json.dumps(row))
        with self.assertRaises(PermissionError):
            service.command(dict(op='incident_list'),uid=os.getuid()+1)
        with self.assertRaises(PermissionError):
            service.command(dict(op='diagnostic_intervention',action='freeze'),uid=os.getuid())


if __name__ == '__main__':
    unittest.main()
