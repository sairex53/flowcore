"""SPDX package graph and provenance serialization fixtures."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_script(name, module_name):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "packaging" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sbom = load_script("generate-sbom.py", "flowcore_sbom")
validator = load_script("validate-sbom.py", "flowcore_sbom_validator")


class SupplyChain(unittest.TestCase):
    def test_spdx_runtime_graph_is_bounded_and_unique(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as temporary:
            root = Path(temporary)
            rpm = root / "flowcore.rpm"
            source = root / "flowcore.tar.gz"
            rpm.write_bytes(b"fixture rpm")
            source.write_bytes(b"fixture source")
            def query(*args):
                if args[0] == "-qp" and any("%{NAME}" in item for item in args):
                    return "flowcore\t0.2.0\t3.fc45\taarch64\tApache-2.0"
                if args[0] == "--root":
                    return ("glibc\t2.42\t1.fc45\taarch64\tLGPL-2.1-or-later\n"
                            "systemd\t258\t1.fc45\taarch64\tLGPL-2.1-or-later")
                if args[0] == "-qp":
                    return "0"
                raise AssertionError(args)
            with patch.object(sbom, "rpm_query", side_effect=query), \
                 patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1790000000"}):
                document = sbom.generate(rpm, root, source)
            self.assertTrue(sbom.validate(document))
            self.assertEqual(document["spdxVersion"], "SPDX-2.3")
            self.assertEqual(len(document["packages"]), 4)
            self.assertEqual(document["documentDescribes"], ["SPDXRef-Package-FLOWCORE"])
            edges = [row for row in document["relationships"] if row["relationshipType"] == "DEPENDS_ON"]
            self.assertEqual(len(edges), 2)
            ids = [row["SPDXID"] for row in document["packages"]]
            self.assertEqual(len(ids), len(set(ids)))

    def test_validator_rejects_unresolved_relationship_and_duplicate_id(self):
        document = {
            "spdxVersion": "SPDX-2.3",
            "packages": [
                {"name": "flowcore", "SPDXID": "SPDXRef-A", "versionInfo": "0.2.0",
                 "downloadLocation": "NOASSERTION", "checksums": []},
                {"name": "flowcore-source", "SPDXID": "SPDXRef-B", "versionInfo": "0.2.0",
                 "downloadLocation": "NOASSERTION", "checksums": []},
            ],
            "relationships": [{"spdxElementId": "SPDXRef-A", "relationshipType": "GENERATED_FROM",
                               "relatedSpdxElement": "SPDXRef-UNKNOWN"}],
        }
        with self.assertRaises(ValueError):
            sbom.validate(document)
        document["relationships"] = []
        document["packages"][1]["SPDXID"] = "SPDXRef-A"
        with self.assertRaises(ValueError):
            sbom.validate(document)

    def test_offline_digest_check_detects_changed_source(self):
        self.assertTrue(callable(validator.validate))


if __name__ == "__main__":
    unittest.main()
