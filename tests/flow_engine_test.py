"""Unprivileged schema tests; the real TCX tests use the isolated root runner."""
import sys
import os
import socket
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from flow_engine import validate_rules,FlowEngine


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

    def test_htb_admission(self):
        class Unowned:
            def validate(self): pass
            def counters(self): return dict(qdisc=[],traffic_classes={})
        with self.assertRaises(RuntimeError):
            FlowEngine.validate_htb(Unowned())

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
