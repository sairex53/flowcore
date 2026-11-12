"""Fail-closed release qualification state and evidence integrity tests."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('release_qualification', ROOT / 'scripts/release_qualification.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def fixture_root(path):
    (path / 'config').mkdir()
    gates = {name: 'PARTIAL' for name in MODULE.GATE_NAMES}
    gates['documentation'] = 'PASS'
    (path / 'config/release-gates.json').write_text(json.dumps({
        'version': 1, 'scope': 'test scope', 'license_approved': True,
        'production_ready': False, 'gates': gates}))


class ReleaseQualificationTests(unittest.TestCase):
    def test_missing_soak_hardware_and_signing_are_blockers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture_root(root)
            report = MODULE.evaluate(root)
            self.assertEqual(report['decision'], 'Release blocked')
            self.assertEqual(report['release_gates']['reliability']['status'], 'BLOCKED')
            self.assertEqual(report['prompt52']['physical_network']['status'], 'BLOCKED')
            self.assertEqual(report['prompt52']['signed_distribution']['status'], 'BLOCKED')
            self.assertFalse(report['production_ready'])

    def test_short_soak_never_qualifies_as_long_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture_root(root)
            (root / 'soak.json').write_text(json.dumps({
                'schema_version': 1, 'status': 'PASS', 'profile': 'full',
                'elapsed_seconds': 900, 'samples': 100,
                'regression': {'resource_gates': {'host_network_unchanged': {'status': 'PASS'}}}}))
            report = MODULE.evaluate(root, soak_report='soak.json')
            self.assertEqual(report['prompt52']['long_soak']['status'], 'UNVERIFIED')
            self.assertEqual(report['decision'], 'Release blocked')

    def test_soak_requires_passed_steps_and_retained_non_symlink_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture_root(root)
            (root / 'logs').mkdir()
            (root / 'logs/service.log').write_text('service completed\n')
            report = {'schema_version': 1, 'status': 'PASS', 'profile': 'service',
                      'elapsed_seconds': 3600, 'samples': 20,
                      'regression': {'resource_gates': {'host_network_unchanged': {'status': 'PASS'}}},
                      'steps': [{'status': 'PASS', 'returncode': 0,
                                 'leaked_process_count': 0, 'log': 'logs/service.log'}]}
            (root / 'soak.json').write_text(json.dumps(report))
            qualified = MODULE._report_status(root, 'soak.json', minimum_seconds=3600)
            self.assertEqual(qualified['status'], 'PASS')
            (root / 'logs/external.log').write_text('outside evidence root\n')
            (root / 'logs/service.log').unlink()
            (root / 'logs/service.log').symlink_to(root / 'logs/external.log')
            rejected = MODULE._report_status(root, 'soak.json', minimum_seconds=3600)
            self.assertEqual(rejected['status'], 'UNVERIFIED')

    def test_validation_manifest_requires_exact_versioned_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture_root(root)
            fields = set(MODULE.PROMPT52_GATES) - {
                'long_soak', 'reboot_and_recovery', 'power_loss'}
            manifest = {'schema_version': 1, **{
                name: {'status': 'UNVERIFIED'} for name in fields}}
            (root / 'manifest.json').write_text(json.dumps(manifest))
            report = MODULE.evaluate(root, validation_manifest='manifest.json')
            self.assertEqual(report['prompt52']['cross_architecture']['status'], 'UNVERIFIED')
            del manifest['concurrency']
            (root / 'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'schema version'):
                MODULE.evaluate(root, validation_manifest='manifest.json')

    def test_source_fingerprint_excludes_its_own_generated_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            subprocess.run(['git', '-C', str(root), 'config', 'user.name', 'FLOWCORE Test'], check=True)
            subprocess.run(['git', '-C', str(root), 'config', 'user.email', 'flowcore-test@example.invalid'], check=True)
            (root / 'config').mkdir()
            (root / 'config/release-gates.json').write_text('{}\n')
            subprocess.run(['git', '-C', str(root), 'add', 'config/release-gates.json'], check=True)
            subprocess.run(['git', '-C', str(root), 'commit', '-qm', 'fixture'], check=True)
            clean = MODULE._git_state(root)
            self.assertFalse(clean['dirty'])
            (root / 'config/release-qualification.json').write_text('{"decision":"Release blocked"}\n')
            self.assertEqual(MODULE._git_state(root), clean)
            (root / 'config/release-gates.json').write_text('{"changed":true}\n')
            self.assertTrue(MODULE._git_state(root)['dirty'])

    def test_reboot_requires_all_lifecycle_checks_and_unchanged_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture_root(root)
            (root / 'reboot.json').write_text(json.dumps({
                'schema_version': 1, 'base_image_unchanged': True,
                'tests': {'initial_boot': {'status': 'PASS'},
                          'clean_reboot': {'status': 'PASS'},
                          'abrupt_qemu_stop': {'status': 'PASS'},
                          'installed_runtime': {'status': 'BLOCKED'}}}))
            report = MODULE.evaluate(root, reboot_report='reboot.json')
            self.assertEqual(report['prompt52']['reboot_and_recovery']['status'], 'BLOCKED')

    def test_evidence_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture_root(root)
            outside = root.parent / (root.name + '-outside.json')
            outside.write_text('{}')
            (root / 'evidence.json').symlink_to(outside)
            try:
                with self.assertRaises(ValueError):
                    MODULE._read_json(root, 'evidence.json')
            finally:
                outside.unlink()


if __name__ == '__main__':
    unittest.main()
