"""Unprivileged installed-runtime input and publication boundary fixtures."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))

from installed_runtime import monotonic_offset, root_file
from result_handoff import destination, publish
from service_config import load


class Security(unittest.TestCase):
    def test_root_path_is_strict_and_clock_is_kernel_reported(self):
        with self.assertRaises(ValueError):
            root_file('/etc/../etc/passwd', 4096)
        self.assertEqual(monotonic_offset(os.getpid()), monotonic_offset('self'))

    def test_sdk_acl_schema(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as raw:
            cfg = Path(raw)/'service.json'
            value = json.loads((ROOT/'config/service.json').read_bytes())
            api = value.setdefault('intent_api', dict(priorities=['standard']))
            api['applications'] = {'api':1001}
            cfg.write_text(json.dumps(value))
            self.assertEqual(load(cfg)['intent_api']['applications'], {'api':1001})
            api['applications']['api'] = True
            cfg.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                load(cfg)

    def test_publication_rejects_symlink_traversal_and_existing_target(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as raw:
            path = Path(raw)
            alias = path.with_name(path.name+'-alias')
            alias.symlink_to(path)
            try:
                for directory in (alias, path/'..'):
                    with self.assertRaises((ValueError, OSError)):
                        with destination(directory, 'output.json'):
                            self.fail('invalid path admitted')
                with self.assertRaises(ValueError):
                    with destination(path, '../escape.json'):
                        self.fail('traversal admitted')
                with destination(path, 'output.json') as fd:
                    (path/'output.json').write_bytes(b'foreign')
                    with self.assertRaises(FileExistsError):
                        publish(fd, 'output.json', {'ok':True})
                self.assertEqual((path/'output.json').read_bytes(), b'foreign')
                self.assertEqual(list(path.glob('.flowcore-*')), [])
                with destination(path, 'new.json') as fd:
                    publish(fd, 'new.json', {'ok':True})
                self.assertEqual((path/'new.json').stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads((path/'new.json').read_text()), {'ok':True})
                with patch('result_handoff.os.geteuid', return_value=0):
                    with self.assertRaises(PermissionError):
                        with destination(path, 'root.json'):
                            self.fail('root publisher admitted')
            finally:
                alias.unlink()


if __name__ == '__main__':
    unittest.main()
