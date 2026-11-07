"""Publication checks are fail-closed; tests never select the project license."""
import importlib.util
from pathlib import Path
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
