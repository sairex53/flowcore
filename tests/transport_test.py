"""Unprivileged transport provenance, quotas, socket attestation and compatibility."""
import os
import socket
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from daemon import Service
from service_config import load
from transport_model import declare, report

class Transport(unittest.TestCase):
    def setUp(self):
        self.service=Service(load(Path(__file__).resolve().parents[1]/'config/service.json'))
        self.api=self.service.intent_api
        self.uid,self.pid=os.getuid(),os.getpid()
        self.token=self.call(action='register',isolation='shared',intent=dict(name='transport_demo',priority='standard'))['lease']
    def call(self,**fields):
        return self.api.call(dict(op='intent_v2',version=2,**fields),self.uid,self.pid)
    def hints(self,kind='quic',cookie=None):
        return dict(session='session',kind=kind,multiplexed=True,mobility_preference=True,cookie=cookie)
    def test_udp_is_not_kernel_attested_quic(self):
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
            observed=self.api.call(dict(op='intent_v2',version=2,action='associate',lease=self.token),
                                   self.uid,self.pid,sock.fileno())
            cookie=int(observed['cookie'])
            result=self.call(action='transport_declare',lease=self.token,transport=self.hints(cookie=cookie))
            self.assertEqual(result['capabilities']['source'],'application_reported')
            self.assertFalse(result['capabilities']['kernel_packet_stream_priority'])
            status=self.call(action='status',lease=self.token)
            self.assertFalse(status['socket_capabilities'][0]['quic_observed'])
            with self.assertRaises(ValueError):
                self.call(action='transport_declare',lease=self.token,transport=self.hints('mptcp',cookie))
    def test_logical_stream_metadata_is_only_application_reported(self):
        import time
        start=time.monotonic_ns()
        event=dict(id=1,phase='start',start_ns=start,end_ns=0,deadline_us=1000,
                   cookie=None,stream_id=4,stream_priority=7)
        self.assertEqual(self.call(action='batch',lease=self.token,events=[event])['accepted'],1)
        end=dict(event,phase='ok',end_ns=time.monotonic_ns())
        self.assertEqual(self.call(action='batch',lease=self.token,events=[end])['accepted'],1)
        row=self.call(action='status',lease=self.token)['stream_reports'][0]
        self.assertEqual(row['stream_id'],4)
        self.assertFalse(row['kernel_priority_enforced'])
        invalid=dict(event,id=2,stream_priority=8)
        self.assertEqual(self.call(action='batch',lease=self.token,events=[invalid])['rejected'],1)

    def test_quota_and_invalid_values(self):
        lease=dict(cookies=set())
        for index in range(16):declare(lease,dict(self.hints(),session=str(index)))
        with self.assertRaises(ValueError):declare(lease,self.hints())
        for value in (dict(self.hints(),kind='http3'),dict(self.hints(),multiplexed=1),
                      dict(self.hints(),session='../bad'),dict(self.hints(),cookie=123)):
            with self.assertRaises(ValueError):declare(dict(cookies=set()),value)
    def test_foreign_lease_and_diagnostic_reporting(self):
        with self.assertRaises(PermissionError):
            self.api.call(dict(op='intent_v2',version=2,action='transport_declare',lease=self.token,
                               transport=self.hints()),self.uid+1,self.pid)
        self.call(action='transport_declare',lease=self.token,transport=self.hints())
        lease=self.api.leases[self.token]
        lease['transport_sessions']['session']['updated_ns']=0
        self.call(action='transport_state',lease=self.token,session='session',degraded=True,error='path')
        self.assertTrue(lease['transport_sessions']['session']['degraded'])
        with self.assertRaises(ValueError):report(lease,'session',True,'arbitrary_error')

if __name__=='__main__':unittest.main()
