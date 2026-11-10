"""Publication checks are fail-closed; tests never select the project license."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_gate", ROOT / "packaging/release-gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

class GateTest(unittest.TestCase):
    def test_license_and_owner_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in gate.DOCS:
                (root / name).write_text("fixture")
            data = dict(version=1, scope="fixture", license_approved=False,
                        production_ready=False, gates={key: "PASS" for key in gate.GATES})
            self.assertFalse(gate.evaluate(root, data)["publication_allowed"])
            (root / "LICENSE").write_text("test fixture only " * 20)
            self.assertFalse(gate.evaluate(root, data)["publication_allowed"])
            data["license_approved"] = True
            self.assertTrue(gate.evaluate(root, data)["publication_allowed"])
            data["gates"]["security"] = "UNVERIFIED"
            self.assertFalse(gate.evaluate(root, data)["publication_allowed"])
            data["gates"]["security"] = "PASS"
            (root / "LICENSE").unlink()
            (root / "README.md").write_text("large fixture " * 20)
            (root / "LICENSE").symlink_to(root / "README.md")
            self.assertFalse(gate.evaluate(root, data)["publication_allowed"])

    @unittest.skipUnless(shutil.which('git'), 'Git is required to test source packaging')
    def test_archive_excludes_untracked_and_rejects_modified_source(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as directory:
            root = Path(directory)
            (root/'packaging').mkdir()
            archive_script = root/'packaging/source-archive.sh'
            shutil.copy2(ROOT/'packaging/source-archive.sh', archive_script)
            cmake = root/'CMakeLists.txt'
            cmake.write_text('project(flowcore VERSION 0.2.0 LANGUAGES CXX)\n')
            git = ['git','-C',directory]
            subprocess.run([*git,'init','-q'],check=True)
            subprocess.run([*git,'add','--','packaging','CMakeLists.txt'],check=True)
            subprocess.run([*git,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                            'commit','-qm','tracked release input'],check=True)
            epoch = subprocess.check_output([*git,'log','-1','--format=%ct'],text=True).strip()
            env = dict(os.environ,SOURCE_DATE_EPOCH=epoch)
            (root/'packaging'/'untracked-fixture').write_text('private fixture')
            output = subprocess.check_output([archive_script],env=env,text=True).strip()
            path = root/output
            first = path.read_bytes()
            with tarfile.open(path) as archive:
                self.assertIn('flowcore-0.2.0/CMakeLists.txt',archive.getnames())
                self.assertFalse(any('untracked-fixture' in name for name in archive.getnames()))
            subprocess.run([archive_script],env=env,check=True,capture_output=True)
            self.assertEqual(path.read_bytes(),first)
            cmake.write_text(cmake.read_text()+'# modified\n')
            rejected = subprocess.run([archive_script],env=env,capture_output=True,text=True)
            self.assertNotEqual(rejected.returncode,0)
            self.assertIn('clean tracked checkout',rejected.stderr)
            self.assertEqual(path.read_bytes(),first)

    def test_invalid_input(self):
        cases = [None, {}, dict(version=True), {"unexpected": 1}]
        valid = dict(version=1, scope="fixture", license_approved=False,
                     production_ready=False, gates={key: "PASS" for key in gate.GATES})
        for key, value in (("version", True), ("license_approved", "yes"),
                           ("production_ready", True), ("scope", []),
                           ("gates", {key: [] for key in gate.GATES})):
            cases.append(dict(valid, **{key: value}))
        for data in cases:
            with self.assertRaises(ValueError):
                gate.evaluate(ROOT, data)
        with self.assertRaises(ValueError):
            gate.unique([("version", 1), ("version", 1)])

if __name__ == "__main__":
    unittest.main()
