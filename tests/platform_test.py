"""Configuration and IPC security tests with deterministic mutation fuzzing."""
import copy
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from ipc_security import authorize, decode, private_directory, private_lock
from platform_network import Inventory
from platform_policy import PolicyStore, load_json, merged, validate


class Platform(unittest.TestCase):
    def policy(self):
        return validate(load_json(ROOT/'config/platform-examples.json'))

    def test_precedence_conflict_and_revision(self):
        policy = self.policy()
        result = merged(policy,'wifi','fc0','api','fc-workload')
        self.assertEqual(result['fields']['target_p99_ms'],35)
        self.assertEqual(result['provenance']['target_p99_ms'],'container')
        self.assertEqual(result['provenance']['background_min_pct'],'interface')
        bad = copy.deepcopy(policy)
        bad['global']['min_kbit'] = 8000
        with self.assertRaises(ValueError):
            validate(bad)
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as work:
            store = PolicyStore()
            store.restore(Path(work))
            self.assertEqual(store.commit(policy,0,123),dict(changed=True,revision=1))
            self.assertFalse(store.commit(policy,1,123)['changed'])
            with self.assertRaises(ValueError):
                store.commit(policy,0,123)
            alternative = copy.deepcopy(policy)
            alternative['global']['target_p99_ms'] = 100
            with patch('platform_policy.os.replace',side_effect=OSError('injected storage failure')):
                with self.assertRaises(OSError):
                    store.commit(alternative,1,123)
            recovered = PolicyStore()
            recovered.restore(Path(work))
            self.assertEqual(recovered.state,store.state)
            self.assertEqual(len(recovered.state['audit']),1)

    def test_private_control_files(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as work:
            root = Path(work)
            private_directory(root)
            lock = root/'lock'
            with private_lock(lock):
                self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
            symlink = root/'alias'
            symlink.symlink_to(lock)
            with self.assertRaises(OSError):
                private_lock(symlink)
            alias = root/'hardlink'
            alias.hardlink_to(lock)
            with self.assertRaises(ValueError):
                private_lock(lock)
            linked_directory = root/'dirlink'
            linked_directory.symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                private_directory(linked_directory)

    def test_ipc_and_authorization(self):
        for payload in (b'{"op":"status","op":"lab_up"}',b'{"op":[]}',b'{"op":"status","number":NaN}',
                        b'{"op":"status","deep":'+b'['*70+b'0'+b']'*70+b'}',
                        b'{"op":"status","many":['+b'0,'*4100+b'0]}'):
            with self.assertRaises(ValueError):
                decode(payload)
        self.assertEqual(decode(b'{"op":"status"}')['op'],'status')
        for op in ('lab_up','controller_start','research_conditions','platform_commit','platform_container_attach'):
            with self.assertRaises(PermissionError):
                authorize(op,True,1000,1000)
        with self.assertRaises(PermissionError):
            authorize('status',False,1001,1000)
        authorize('status',True,1000,1000)

    def test_seeded_parser_mutations(self):
        rng = random.Random(40211)
        inputs = [b'{"op":"platform_commit","policy":{"version":1}}',
                  json.dumps(self.policy()).encode(),b'{"op":"health"}']
        for _ in range(2000):
            payload = bytearray(rng.choice(inputs))
            for _ in range(rng.randrange(1,8)):
                payload[rng.randrange(len(payload))] = rng.randrange(256)
            try:
                row = decode(payload)
            except (ValueError,UnicodeError):
                continue
            self.assertIsInstance(row['op'],str)
            if 'policy' in row:
                try:
                    validate(row['policy'])
                except ValueError:
                    pass

    def test_real_inventory_without_privileges(self):
        inventory = Inventory(PolicyStore())
        inventory.refresh()
        view = inventory.status()
        self.assertIsNone(view['error'])
        self.assertTrue(any(row['kind']=='loopback' for row in view['interfaces']))
        self.assertTrue(all(row['path_quality']['end_to_end_loss_ratio'] is None for row in view['interfaces']))


if __name__=='__main__':
    unittest.main()
