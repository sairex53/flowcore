import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from workloads import Workloads
from platform_policy import EMPTY, merged, validate

IDENTITY = dict(path='/tests/api',id=12,network_namespace=99,process_start_ticks='123',uid=1000,boot_id='test')

class WorkloadTests(unittest.TestCase):
    @patch('workloads.cgroup',return_value=IDENTITY)
    def test_identity_lifetime_owner_and_duplicate_names(self, read):
        registry=Workloads()
        a=registry.register(1000,101,'api')
        b=registry.register(1000,102,'api')
        self.assertNotEqual(a['id'],b['id'])
        self.assertFalse(a['network_authority'])
        self.assertEqual(registry.view(1001)['workloads'],[])
        with self.assertRaises(PermissionError):
            registry.register(1001,101,'api')
        with self.assertRaises(PermissionError):
            registry.forget(1001,a['id'])
        read.return_value=dict(IDENTITY,process_start_ticks='999')
        registry.refresh()
        self.assertTrue(all(row['state']=='stale' for row in registry.rows.values()))

    @patch('workloads.cgroup',return_value=IDENTITY)
    def test_quota_and_namespace_migration(self,read):
        registry=Workloads()
        for pid in range(1,33):
            registry.register(1000,pid,'api')
        with self.assertRaises(ValueError):
            registry.register(1000,33,'api')
        read.return_value=dict(IDENTITY,network_namespace=100)
        registry.refresh()
        self.assertEqual(len(registry.view(1000)['events']),32)

    def test_cgroup_inheritance_and_conflicts(self):
        policy=copy.deepcopy(EMPTY)
        policy['global']={'min_kbit':100}
        policy['cgroup']={'/tests/api':{'max_kbit':200}}
        validate(policy)
        row=merged(policy,cgroup='/tests/api')
        self.assertEqual(row['fields']['min_kbit'],100)
        self.assertEqual(row['provenance']['max_kbit'],'cgroup')
        policy['cgroup']['/tests/api']['max_kbit']=50
        with self.assertRaises(ValueError): validate(policy)
        for path in ('relative','/../api','/test//api'):
            policy=copy.deepcopy(EMPTY);policy['cgroup']={path:{}}
            with self.assertRaises(ValueError): validate(policy)

if __name__=='__main__': unittest.main()
