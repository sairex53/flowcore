"""Deterministic request/config mutation and fail-closed unit checks."""
import json
import random
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from network_helper import Actuator, LIMIT, Rejected, config, request, root_config


class Helper(unittest.TestCase):
    def test_protocol(self):
        base = dict(version=1, op='queue.set', epoch='a'*32, revision=0, limit_packets=42)
        self.assertEqual(request(json.dumps(base).encode()), base)
        for updates in (dict(version=True), dict(op='shell'), dict(path='../../etc'),
                        dict(uid=0), dict(service='operator'), dict(limit_packets=True),
                        dict(limit_packets=4097), dict(revision=-1), dict(epoch='../eth0')):
            with self.assertRaises(Rejected):
                request(json.dumps(dict(base, **updates)).encode())
        for raw in (b'{'*LIMIT, b'x'*(LIMIT+1), b'{"op":"status","op":"lab.remove"}',
                    b'{"op":"status","version":1e9999}'):
            with self.assertRaises(Rejected):
                request(raw)

    def test_acl_and_failure(self):
        net = Mock(tag=b'flowcore:' + b'a'*32, indices=None)
        act = Actuator(net, dict(operators=[12], readers=[13]))
        self.assertFalse(act.run(13, {'op': 'status'})['lab_up'])
        create = dict(op='lab.create', epoch='a'*32, revision=0)
        with self.assertRaisesRegex(Rejected, 'forbidden'):
            act.run(13, create)
        with self.assertRaisesRegex(Rejected, 'stale_revision'):
            act.run(12, dict(create, revision=1))
        net.create.assert_not_called()
        net.create.side_effect = OSError('injected kernel error')
        with patch('network_helper.audit'), self.assertRaisesRegex(Rejected, 'network_failed'):
            act.run(12, create)
        self.assertTrue(act.failed)
        self.assertEqual(act.revision, 0)
        self.assertFalse(act.view()['verified'])
        self.assertIsNone(act.view()['lab_up'])
        with self.assertRaisesRegex(Rejected, 'network_failed'):
            act.run(12, create)
        net.create.assert_called_once()
        net.remove.assert_not_called()

    def test_config_and_paths(self):
        self.assertEqual(config(b'{"version":1,"operators":[42],"readers":[]}')['operators'], [42])
        for raw in (b'{}', b'{"version":1,"operators":[0],"readers":[]}',
                    b'{"version":1,"operators":[42,42],"readers":[]}',
                    b'{"version":1,"operators":[true],"readers":[]}',
                    b'{"version":1,"operators":[],"readers":[],"include":"/tmp"}'):
            with self.assertRaises(ValueError):
                config(raw)
        for name in ('../etc/passwd', '/etc/../etc/passwd', '//etc/passwd'):
            with self.assertRaises(ValueError):
                root_config(name)
        with self.assertRaises(OSError):
            root_config('/proc/self/exe')  # intermediate symlink

    def test_seeded_mutations(self):
        rng = random.Random(9234)
        for decoder, exception, seed in (
            (request, Rejected, b'{"version":1,"op":"status"}'),
            (config, (ValueError, RecursionError), b'{"version":1,"operators":[12],"readers":[]}'),
        ):
            for _ in range(2000):
                data = bytearray(seed)
                for _ in range(rng.randint(1, 8)):
                    data[rng.randrange(len(data))] = rng.randrange(256)
                try:
                    result = decoder(data)
                except exception:
                    continue
                self.assertEqual(result['version'], 1)


if __name__ == '__main__':
    unittest.main()
