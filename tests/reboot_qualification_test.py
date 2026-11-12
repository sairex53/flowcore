"""Unit checks for safe disposable VM command construction and preflight."""
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('reboot_qualification', ROOT / 'scripts/reboot_qualification.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RebootQualificationTests(unittest.TestCase):
    def test_qemu_is_tcg_loopback_and_network_restricted(self):
        argv = MODULE.build_qemu_argv('/usr/bin/qemu-system-aarch64', Path('/tmp/base.qcow2'),
                                      Path('/tmp/overlay.qcow2'), Path('/tmp/vars.fd'),
                                      Path('/tmp/code.fd'), 29999, 4096, 4,
                                      Path('/tmp/serial.log'), Path('/tmp/qmp.sock'))
        self.assertIn('virt,accel=tcg', argv)
        netdev = argv[argv.index('-netdev') + 1]
        self.assertIn('restrict=on', netdev)
        self.assertIn('hostfwd=tcp:127.0.0.1:29999-:22', netdev)
        self.assertNotIn('tap', netdev)
        self.assertIn('readonly=on', argv[argv.index('-drive') + 1])

    def test_invalid_preflight_does_not_create_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / 'not-qcow2'
            base.write_text('test')
            key = root / 'id_ed25519'
            key.write_text('private-test-material')
            key.chmod(0o600)
            output = root / 'not-created'
            args = SimpleNamespace(base_image=base, output_dir=output,
                ssh_key=key, disposable_copy=True,
                firmware_code=Path('/usr/share/AAVMF/AAVMF_CODE.fd'),
                firmware_vars=Path('/usr/share/AAVMF/AAVMF_VARS.fd'),
                memory_mb=4096, cpus=4, boot_timeout=300, reboot_timeout=300)
            with self.assertRaises(ValueError):
                MODULE._validate_inputs(args)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
