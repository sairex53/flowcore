"""Unprivileged schema tests; the real TCX tests use the isolated root runner."""
import copy
import sys
import os
import socket
import unittest
from unittest.mock import Mock, patch
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from flow_engine import validate_rules,validate_page,FlowEngine


class Schema(unittest.TestCase):
    def test_fallback_and_intervals(self):
        from flow_snapshot import snapshot
        from research_stats import median_ci
        self.assertEqual(median_ci(list(range(1,11)))['low'],2)
        self.assertEqual(median_ci(list(range(1,11)))['high'],9)
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
            sock.bind(('127.0.0.1',0))
            result=snapshot(4096)
            self.assertTrue(any(row['protocol']==17 and row['local_port']==sock.getsockname()[1]
                                for row in result['flows']))
            self.assertTrue(result['degraded'])
        if os.geteuid()!=0:
            self.assertFalse(FlowEngine().start(None))

    def test_synthetic_paired_effect(self):
        from flow_engine_benchmark import aggregate
        rows=[dict(mode=mode,repetition=rep,p99_ms=latency,total_mbps=20,
                   background_mbps=10,host_busy_cpu_seconds=1,engine_cpu_seconds=0,
                   offered=100,failed=0) for rep in range(10)
              for mode,latency in (('none',20),('flower',10),('bpf',5))]
        effect=aggregate(rows)['paired']['flower_p99_ms']
        self.assertEqual(effect['median_percent'],-50)
        self.assertEqual((effect['median_ci']['low'],effect['median_ci']['high']),(-50,-50))

    @staticmethod
    def pages(total=600):
        rows=[dict(type='flow',session_ns=1,netns=1,ifindex=2,sport=1000+i,dport=9000,
                   first_ns=1,last_ns=2,family=2,protocol=17,source='10.0.0.1',
                   destination='10.0.0.2',cookie=str(i+1),vlan_depth=0,
                   vlan_id=[0,0],vlan_proto=[0,0]) for i in range(total)]
        return [dict(type='page',protocol=2,snapshot=1,session_ns=1,captured_ns=3,
                     cursor=i,total=total,next=i+4 if i+4<total else None,flows=rows[i:i+4])
                for i in range(0,total,4)]

    def test_page_validation(self):
        page=self.pages(8)[0]
        self.assertEqual(len(validate_page(page,0)),4)
        changes=[dict(cursor=4),dict(snapshot=0),dict(total=4097),dict(next=True),
                 dict(protocol=2.0),dict(flows=page['flows'][:3])]
        for change in changes:
            with self.assertRaises(ValueError): validate_page(dict(page,**change),0)
        for field,value in [('cookie',str(2**64)),('last_ns',4),('family',2.0),
                            ('source','::1'),('vlan_id',[4096,0])]:
            malformed=copy.deepcopy(page);malformed['flows'][0][field]=value
            with self.assertRaises(ValueError): validate_page(malformed,0)
        with self.assertRaises(ValueError): validate_page(page,0,2)

    def test_full_export_and_failures(self):
        class Fake(FlowEngine):
            def __init__(self,pages):
                super().__init__();self.pages=iter(pages)
            def send(self,message):
                try: self.records.append(next(self.pages))
                except StopIteration: raise RuntimeError('injected disconnect') from None
            def wait(self,predicate,timeout=1):
                if not predicate(): raise TimeoutError('missing page')
        pages=self.pages()
        engine=Fake(pages)
        self.assertEqual(len(engine.dump()),600)
        self.assertTrue(engine.last_snapshot['complete'])
        for broken in (pages[:1],copy.deepcopy(pages)):
            if len(broken)>1:
                broken[1]['session_ns']=2
            engine=Fake(broken)
            with self.assertRaises((RuntimeError,ValueError)): engine.dump()
            self.assertFalse(engine.last_snapshot['complete'])
            self.assertEqual(engine.export_failures,1)
        duplicate=copy.deepcopy(pages)
        duplicate[1]['flows'][0]=copy.deepcopy(duplicate[0]['flows'][0])
        with self.assertRaises(ValueError): Fake(duplicate).dump()

    def test_htb_admission(self):
        class Unowned:
            def validate(self): pass
            def counters(self): return dict(qdisc=[],traffic_classes={})
        with self.assertRaises(RuntimeError):
            FlowEngine.validate_htb(Unowned())

    def test_lost_policy_ack_revokes_engine(self):
        engine=FlowEngine()
        engine.lab=Mock(assert_htb_layout=Mock())
        engine.ready=True
        engine.generation=1
        with (patch.object(engine, 'validate_htb'),
              patch.object(engine, 'send') as send,
              patch.object(engine, 'wait', side_effect=TimeoutError)):
            with self.assertRaisesRegex(RuntimeError,'detached'):
                engine.set_policy(2,[])
        send.assert_called_once()
        self.assertFalse(engine.ready)
        self.assertIsNone(engine.generation)
        self.assertIsNone(engine.lab)

    def test_strict_rules(self):
        rule=dict(protocol=6,dport=9000,service=1,classid=0x10010)
        self.assertEqual(validate_rules([rule])[0]['cookie'],0)
        for change in (dict(protocol=1),dict(dport=-1),dict(service=True),
                       dict(classid=0x10000),dict(cookie=2**64),dict(extra='shell')):
            with self.assertRaises(ValueError):
                validate_rules([dict(rule,**change)])
        with self.assertRaises(ValueError):
            validate_rules([rule]*17)
        engine=FlowEngine()
        self.assertFalse(engine.ready)
        with self.assertRaises(RuntimeError):
            engine.send('dump')


if __name__=='__main__':
    unittest.main()
