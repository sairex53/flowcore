"""Pure regression checks for remote lab input, scope, and path evidence."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from multi_host_agent import OwnedQueue
from multi_host_lab import HANDLES, manifest, validate_path


def counters(root, critical, interactive, background):
    values = dict(critical=critical, interactive=interactive, standard=0, background=background)
    rows = [dict(handle='1:', root=True, bytes=root)]
    rows += [dict(handle=HANDLES[name], bytes=count) for name, count in values.items()]
    return {'queues': {'qdisc': rows}}


class LabTest(unittest.TestCase):
    def test_manifest_rejects_untrusted_target_and_nonfinite_value(self):
        cfg = json.loads((ROOT / 'config/multi-host.example.json').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps(cfg))
            self.assertEqual(manifest(path)['roles']['client']['ssh'], 'lab-client')
            cfg['roles']['client']['ssh'] = '-oProxyCommand=arbitrary'
            path.write_text(json.dumps(cfg))
            with self.assertRaises(ValueError):
                manifest(path)
            cfg['roles']['client']['ssh'] = 'lab-client'
            cfg['workload']['api_rate'] = float('nan')
            path.write_text(json.dumps(cfg))
            with self.assertRaises(ValueError):
                manifest(path)

    def test_initial_namespace_rejected_before_interface_access(self):
        with (patch('multi_host_agent.os.geteuid', return_value=0),
              patch('multi_host_agent.os.stat', return_value=object()),
              patch('multi_host_agent.link') as probe):
            with self.assertRaisesRegex(PermissionError, 'dedicated network namespace'):
                OwnedQueue('data0', 'example')
            probe.assert_not_called()

    def test_path_requires_correct_class_and_nonreset_counters(self):
        transfer = {'error': None, 'bytes_expected': 1000, 'bytes_transferred': 1000}
        before = counters(20, 10, 10, 0)
        good = counters(1200, 100, 100, 1000)
        reset = counters(1100, 9, 100, 1000)
        bypass = counters(1200, 100, 100, 999)
        self.assertEqual(validate_path('static', before, good, transfer)['state'], 'observed')
        for state in (reset, bypass):
            self.assertEqual(validate_path('static', before, state, transfer)['state'],
                             'missing_path_evidence')


if __name__ == '__main__':
    unittest.main()
