"""Deterministic evidence, safety and Linux UAPI decoding regression tests."""
import ctypes
import socket
import struct
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from dynamic_path_lab import EndpointWriter
from multipath import full_subflows
from path_adaptation import PathController, PathEvidence, PathIntent, advisory, load_intent

NANO=1_000_000_000


def observed(t, capacity=12000, size=0, backlog=0, available=True, identity='route1', competing=0):
    return dict(sample_ns=int(t*NANO),identity=identity,departed_bytes=size,queue_drops=0,
                backlog_bytes=backlog,capacity_kbit=capacity,available=available,probe_ms=4 if available else None,
                competing_bytes=competing,location='MANAGED_INTERMEDIATE')


def candidate(ident, when, *, capacity, available=True, confirmed=True, event=None, epoch=1, pressure=False):
    return dict(path_id=ident, sample_ns=int(when*NANO), capacity_kbit=capacity, available=available,
                route_valid=available, subflow_observed=confirmed, probe_ms=4 if ident==1 else 20,
                probe_samples=3, probe_p50_ms=4 if ident==1 else 20, probe_failures=0,
                pressure=pressure, epoch=epoch, recent_change=event)


class EvidenceTest(unittest.TestCase):
    def test_idle_and_counter_reset_do_not_measure_capacity(self):
        model=PathEvidence()
        for t in (1,2,3,4):
            row=model.observe(observed(t))
        self.assertIsNone(row['saturated_rate_kbit'])
        self.assertEqual(row['capacity_kbit'],12000)
        for t in (5,6,7):
            row=model.observe(observed(t,size=(t-4)*1_450_000,backlog=10000))
        self.assertAlmostEqual(row['saturated_rate_kbit'],11600)
        self.assertEqual(row['location'],'MANAGED_INTERMEDIATE')
        row=model.observe(observed(8,capacity=10800,size=5_550_000,backlog=10000))
        self.assertIsNone(row['saturated_rate_kbit'])
        row=model.observe(observed(9,identity='route2'))
        self.assertEqual(row['event'],'BASELINE')
        self.assertIsNone(row['saturated_rate_kbit'])

    def test_observed_capacity_transitions_and_failure(self):
        model=PathEvidence()
        plan=[(12000,True,'BASELINE'),(10800,True,'CAPACITY_CHANGE'),(9600,True,'GRADUAL_DEGRADATION'),
              (1500,True,'SUDDEN_REDUCTION'),(1500,False,'INTERFACE_FAILURE'),
              (1500,True,'LINK_RECOVERY'),(12000,True,'CAPACITY_RECOVERY')]
        for t,(rate,up,event) in enumerate(plan,1):
            self.assertEqual(model.observe(observed(t,rate,available=up))['event'],event)

    def test_rtt_spread_requires_multiple_successful_probes(self):
        model=PathEvidence()
        first=model.observe(observed(1))
        self.assertIsNone(first['rtt_spread_ms'])
        second=model.observe(dict(observed(2),probe_ms=10))
        self.assertEqual(second['rtt_spread_ms'],6)

    def test_recovery_time_requires_a_successful_post_recovery_probe(self):
        model=PathEvidence()
        model.observe(observed(1))
        model.observe(observed(2,available=False))
        model.observe(observed(3,available=False))
        no_probe=model.observe(dict(observed(4),probe_ms=None))
        self.assertEqual(no_probe['event'],'LINK_RECOVERY')
        self.assertIsNone(no_probe['recovery_ms'])
        recovered=model.observe(observed(5))
        self.assertEqual(recovered['recovery_samples'],1)
        self.assertEqual(recovered['recovery_ms'],3000.0)
        self.assertEqual(recovered['recovery_source'],'sampled_link_down_to_successful_probe')

    def test_route_identity_change_discards_unrelated_recovery_history(self):
        model=PathEvidence()
        model.observe(observed(1))
        model.observe(observed(2,available=False))
        row=model.observe(observed(3,identity='route2'))
        self.assertIsNone(row['recovery_ms'])
        self.assertEqual(row['recovery_samples'],0)

    def test_competing_demand_requires_pressure_and_history(self):
        model=PathEvidence()
        for t in (1,2,3):
            model.observe(observed(t,size=t*500000,competing=t*100000))
        row=model.observe(observed(4,size=3_000_000,competing=1_000_000,backlog=10000))
        self.assertEqual(row['event'],'COMPETING_DEMAND')


class ControllerTest(unittest.TestCase):
    def test_migration_requires_verified_subflow_and_distinct_windows(self):
        policy=PathController(PathIntent())
        bad=candidate(1,2,capacity=1500,pressure=True,event='SUDDEN_REDUCTION')
        alt=candidate(2,2,capacity=8000,confirmed=False)
        self.assertEqual(policy.evaluate(2*NANO,{1:bad,2:alt},{})['action'],'NO_INTERVENTION')
        alt=dict(alt,subflow_observed=True,sample_ns=3*NANO)
        bad=dict(bad,sample_ns=3*NANO)
        self.assertEqual(policy.evaluate(3*NANO,{1:bad,2:alt},{})['action'],'NO_INTERVENTION')
        self.assertEqual(policy.evaluate(3*NANO,{1:bad,2:alt},{})['action'],'NO_INTERVENTION')
        alt,bad=dict(alt,sample_ns=4*NANO),dict(bad,sample_ns=4*NANO)
        decision=policy.evaluate(4*NANO,{1:bad,2:alt},{})
        self.assertEqual(decision['chosen_path_id'],2)
        self.assertEqual(policy.primary,1)
        policy.commit(2,4*NANO)
        self.assertEqual(policy.primary,2)
        self.assertEqual(policy.evaluate(6*NANO,{1:bad,2:alt},{})['action'],'NO_INTERVENTION')

    def test_healthy_path_holds_and_recovery_requires_current_subflow_evidence(self):
        policy=PathController(PathIntent())
        current=candidate(1,2,capacity=12000)
        alternate=candidate(2,2,capacity=8000)
        self.assertEqual(policy.evaluate(2*NANO,{1:current,2:alternate},{'deadline_misses':8,'samples':10})['action'],
                         'NO_INTERVENTION')
        current=dict(current,available=False,sample_ns=3*NANO)
        alternate=dict(alternate,sample_ns=3*NANO)
        switch=policy.evaluate(3*NANO,{1:current,2:alternate},{})
        self.assertEqual(switch['chosen_path_id'],2)
        policy.commit(2,3*NANO)
        recovered=dict(current,available=True,subflow_observed=False,recent_change='LINK_RECOVERY',sample_ns=5*NANO)
        alternate=dict(alternate,sample_ns=5*NANO)
        self.assertEqual(policy.evaluate(5*NANO,{1:recovered,2:alternate},{})['action'],'NO_INTERVENTION')
        self.assertEqual(policy.evaluate(8*NANO,{1:recovered,2:alternate},{})['action'],'NO_INTERVENTION')

    def test_path_spread_and_sample_stability_break_equal_latency_ties(self):
        policy=PathController(PathIntent(preference='reliability',deadline_ms=20))
        current=candidate(1,2,capacity=8000,pressure=True)
        current.update(probe_p50_ms=4,probe_samples=5,probe_failures=0,probe_stability=1.0,
                       rtt_spread_ms=3,recovery_ms=20,recovery_samples=1)
        alternate=candidate(2,2,capacity=8000)
        alternate.update(probe_p50_ms=4,probe_samples=5,probe_failures=0,probe_stability=1.0,
                         rtt_spread_ms=1,recovery_ms=10,recovery_samples=1)
        self.assertEqual(policy.evaluate(2*NANO,{1:current,2:alternate},{})['action'],'NO_INTERVENTION')
        current=dict(current,sample_ns=3*NANO)
        alternate=dict(alternate,sample_ns=3*NANO)
        decision=policy.evaluate(3*NANO,{1:current,2:alternate},{})
        self.assertEqual(decision['chosen_path_id'],2)
        self.assertEqual(decision['candidate_metrics']['recovery_ms'],10)

    def test_return_after_recovery_requires_three_distinct_windows(self):
        policy=PathController(PathIntent())
        primary=candidate(1,2,capacity=12000)
        backup=candidate(2,2,capacity=8000)
        primary=dict(primary,available=False,route_valid=False)
        switched=policy.evaluate(2*NANO,{1:primary,2:backup},{})
        self.assertEqual(switched['chosen_path_id'],2)
        policy.commit(2,2*NANO)
        for when in (4,5):
            restored=candidate(1,when,capacity=12000,event='LINK_RECOVERY')
            standby=candidate(2,when,capacity=8000)
            result=policy.evaluate(when*NANO,{1:restored,2:standby},{})
            self.assertEqual(result['action'],'NO_INTERVENTION')
            self.assertEqual(result['recovery_confirmations_required'],3)
        restored=candidate(1,6,capacity=12000,event='LINK_RECOVERY')
        standby=candidate(2,6,capacity=8000)
        result=policy.evaluate(6*NANO,{1:restored,2:standby},{})
        self.assertEqual(result['action'],'MPTCP_BACKUP_PREFERENCE')

    def test_cooperative_intent_schema(self):
        path=Path(__file__).resolve().parents[1]/'config'/'path-intent.json'
        label,intent=load_intent(path)
        self.assertEqual((label,intent.preference,intent.deadline_ms),('interactive-api','reliability',100))
        with self.assertRaises(ValueError):
            PathIntent(preference='unknown')

    def test_no_multipath_and_unowned_causes_hold(self):
        policy=PathController(PathIntent(multipath=False))
        paths={1:candidate(1,2,capacity=1500,available=False),2:candidate(2,2,capacity=8000)}
        self.assertEqual(policy.evaluate(2*NANO,paths,{})['action'],'NO_INTERVENTION')
        for cause in ('REMOTE_PATH_SUSPECTED','RECEIVER_LIMITED_SUSPECTED','APPLICATION_LIMITED_SUSPECTED'):
            self.assertEqual(advisory(dict(state='CONGESTED',classification=cause,confidence='suspected'))['action'],
                             'NO_INTERVENTION')
        local=dict(state='CONGESTED',classification='LOCAL_EGRESS_CONGESTION',confidence='observed_local',
                   directions={'egress':dict(controllable=True,qdisc='htb')})
        self.assertEqual(advisory(local)['action'],'HTB_ADJUSTMENT')
        self.assertEqual(advisory(local,authorized_datagram=True)['action'],'PER_MESSAGE_PRIORITIZATION')
        self.assertEqual(advisory(local,fq_socket=True)['action'],'SOCKET_PACING')

    def test_endpoint_transition_compensates_partial_write(self):
        state={1:False,2:True}
        lab=SimpleNamespace(assert_router=lambda:None)
        writer=EndpointWriter(lab,[dict(id=1),dict(id=2)],state.copy())
        writer.expected=state.copy()
        def write(_lab,path,backup,_owned):
            state[path['id']]=backup
            if path['id']==1 and backup:
                raise RuntimeError('injected readback failure after write')
        with (patch.object(writer,'read',side_effect=lambda:state.copy()),
              patch('dynamic_path_lab.valid_route',return_value=True),
              patch('dynamic_path_lab.mp._link_up',return_value=True),
              patch('dynamic_path_lab.mp.set_endpoint_backup',side_effect=write) as op):
            result=writer.apply(2)
            self.assertEqual(result['rollback'],'verified')
            self.assertEqual(state,{1:False,2:True})
            self.assertEqual(op.call_count,4)
            state[2]=False
            with self.assertRaises(RuntimeError):
                writer.apply(2)
            self.assertEqual(op.call_count,4)

    def test_full_info_kernel_clamped_stride(self):
        def getsockopt(fd,level,option,header,length):
            _,_,_,_,_,count,addr,tcp=struct.unpack_from('=6I2Q',header)
            self.assertEqual((fd,level,option),(15,284,4))
            struct.pack_into('=6I',header,0,144,144,264,264,2,count)
            for index in range(2):
                payload=bytearray(264)
                struct.pack_into('=I',payload,0,index)
                for offset,host in ((8,'10.0.0.1'),(136,'10.0.0.2')):
                    struct.pack_into('=H',payload,offset,socket.AF_INET)
                    struct.pack_into('!H',payload,offset+2,9017+index)
                    payload[offset+4:offset+8]=socket.inet_aton(host)
                ctypes.memmove(addr+index*264,bytes(payload),264)
                info=bytearray(144)
                struct.pack_into('=I',info,68,1000+index*4000)
                ctypes.memmove(tcp+index*144,bytes(info),144)
            return 0
        with patch('multipath.ctypes.CDLL',return_value=SimpleNamespace(getsockopt=getsockopt)):
            rows=full_subflows(SimpleNamespace(fileno=lambda:15))
        self.assertEqual([x['tcp_info']['rtt_us'] for x in rows],[1000,5000])
        self.assertEqual([x['remote_port'] for x in rows],[9017,9018])


if __name__=='__main__':
    unittest.main()
