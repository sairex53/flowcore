"""Durability fault injection and conservative reload invariants."""
import copy
import errno
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from durable_state import atomic_json, read_json, DurabilityUncertain
from operations import Operations
from service_config import load

class Deployment(unittest.TestCase):
    def test_failed_write_preserves_old_and_uncertain_commit_is_visible(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as temp:
            path=Path(temp)/'state.json'
            atomic_json(path,dict(generation=1))
            with patch('durable_state.os.fsync',side_effect=OSError(errno.ENOSPC,'injected full disk')):
                with self.assertRaises(OSError):atomic_json(path,dict(generation=2))
            self.assertEqual(read_json(path)['generation'],1)
            committed=[]
            with patch('durable_state.os.fsync',side_effect=[None,OSError(errno.EIO,'directory fsync failed')]):
                with self.assertRaises(DurabilityUncertain):
                    atomic_json(path,dict(generation=2),on_commit=lambda:committed.append(True))
            self.assertEqual(committed,[True])
            self.assertEqual(read_json(path)['generation'],2)
            self.assertEqual(list(Path(temp).glob('.state-*')),[])
    def test_symlinks_and_permission_failures(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as temp:
            directory=Path(temp);target=directory/'target';target.write_text('{}')
            alias=directory/'alias';alias.symlink_to(target)
            with self.assertRaises(OSError):read_json(alias)
            with self.assertRaises(PermissionError):atomic_json(alias,{})
            target.chmod(0o666)
            with self.assertRaises(PermissionError):read_json(target)
            with patch('durable_state.os.open',side_effect=PermissionError('injected denial')):
                with self.assertRaises(PermissionError):atomic_json(directory/'new',{})
    def test_source_config_owner_exception_does_not_relax_installed_or_state(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as temp:
            config=Path(temp)/'service.json'
            config.write_text((ROOT/'config/service.json').read_text())
            with patch('durable_state.os.geteuid',return_value=123456):
                with self.assertRaises(PermissionError):read_json(config)
                with patch('paths.INSTALLED',False):
                    self.assertEqual(load(config)['version'],1)
                with patch('paths.INSTALLED',True):
                    with self.assertRaises(PermissionError):load(config)

    def test_reload_has_explicit_compatibility_and_rollback(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as temp:
            directory=Path(temp);config=directory/'service.json'
            original=json.loads((ROOT/'config/service.json').read_text())
            config.write_text(json.dumps(original))
            service=SimpleNamespace(config=load(config))
            ops=Operations(service,directory/'recovery.json',config);ops.start()
            candidate=copy.deepcopy(original);candidate['telemetry_seconds']=3
            config.write_text(json.dumps(candidate))
            self.assertTrue(ops.reload(True)['dry_run'])
            self.assertEqual(ops.checkpoint['generation'],0)
            ops.reload();self.assertEqual(service.config['telemetry_seconds'],3)
            candidate['lab']['bandwidth_kbit']+=100
            config.write_text(json.dumps(candidate))
            with self.assertRaises(ValueError):ops.reload()
            self.assertEqual(service.config['telemetry_seconds'],3)
            self.assertEqual(ops.checkpoint['generation'],1)
            config.write_text('{')
            with self.assertRaises(ValueError):ops.reload()
            recovered=Operations(service,directory/'recovery.json',config);recovered.start()
            self.assertTrue(recovered.previous_unclean)
            recovered.finish()
            clean=Operations(service,directory/'recovery.json',config);clean.start()
            self.assertFalse(clean.previous_unclean)
if __name__=='__main__':unittest.main()
