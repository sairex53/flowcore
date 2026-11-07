"""Unprivileged configuration and CLI tests."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('FLOWCORE_TEST_BINARY', ROOT / 'build/flowcore'))
sys.path.insert(0, str(ROOT / 'scripts'))
from service_config import load
from service_lab import ServiceLab
from network_state import snapshot


class Unit(unittest.TestCase):
    def test_configuration(self):
        original = json.loads((ROOT / 'config/service.json').read_text())
        with tempfile.TemporaryDirectory(dir=BINARY.parent) as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(original))
            self.assertEqual(load(path)['version'], 1)
            for mutate in (
                lambda cfg: cfg.update(version=True),
                lambda cfg: cfg.update(interface='eth0'),
                lambda cfg: cfg['lab'].update(delay_ms=0, jitter_ms=2),
                lambda cfg: cfg['lab'].update(loss_pct=float('nan')),
                lambda cfg: cfg['policies']['default']['classes']['critical'].update(min_pct=99),
                lambda cfg: cfg['policies']['default']['classes']['critical'].update(port=9001),
            ):
                cfg = json.loads(json.dumps(original))
                mutate(cfg)
                path.write_text(json.dumps(cfg))
                with self.assertRaises(ValueError):
                    load(path)
            path.write_text('{"version":1,"version":1}')
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                load(path)

    def test_invalid_tc_readback_is_identified(self):
        lab = ServiceLab.__new__(ServiceLab)
        lab.tc = lambda *args: 'unexpected output'
        with self.assertRaisesRegex(RuntimeError, 'tc qdisc readback is not JSON'):
            lab.counters()

    def test_interfaces(self):
        result = snapshot()
        loopback = next(link for link in result['interfaces'] if link['name'] == 'lo')
        self.assertGreater(loopback['mtu'], 0)
        self.assertTrue(any(addr['address'] == '127.0.0.1' for addr in loopback['addresses']))
        self.assertTrue(result['routes'])

    def test_cli(self):
        binary = BINARY
        for words in ([], ['status'], ['interfaces'], ['metrics'], ['daemon', 'status'],
                      ['observe'], ['connections'], ['congestion'], ['events'], ['health'],
                      *(['report', verb] for verb in ('start', 'sample', 'complete')),
                      *(['lab', verb] for verb in ('up', 'down', 'status', 'configure', 'inspect')),
                      *(['policy', verb] for verb in ('list', 'apply', 'inspect', 'evaluate', 'explain', 'history')),
                      *(['intent', verb] for verb in ('register', 'list', 'inspect', 'remove')),
                      *(['controller', verb] for verb in ('start', 'stop', 'status', 'shadow'))):
            result = subprocess.run([binary, *words, '--help'], capture_output=True)
            self.assertEqual(result.returncode, 0, words)
            self.assertIn(b'usage:', result.stdout)
        self.assertEqual(subprocess.run([binary, 'policy', 'apply'], capture_output=True).returncode, 2)
        self.assertIn('interfaces', json.loads(subprocess.check_output([binary, 'interfaces', '--local'])))


if __name__ == '__main__':
    unittest.main()
