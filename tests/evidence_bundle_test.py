#!/usr/bin/env python3
"""Exercise the evidence bundle CLI in isolated stdlib-only mode."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "packaging" / "evidence_bundle.py"


def main():
    with tempfile.TemporaryDirectory(prefix="flowcore-standalone-") as directory:
        result = subprocess.run(
            [sys.executable, "-I", str(TOOL), "self-test"],
            cwd=directory, capture_output=True, text=True, timeout=30, check=True)
        summary = json.loads(result.stdout)
    assert summary["status"] == "PASS"
    assert summary["archive_roundtrip"] == "PASS"
    assert summary["checksum_tamper"] == "REJECTED"
    assert summary["archive_path_traversal"] == "REJECTED"
    assert summary["preserved_failed_requests"] == 1
    metadata = json.loads((ROOT / "packaging" / "evidence-release-metadata.json").read_text())
    assert metadata["status"] == "LOCAL_CANDIDATE_NOT_UPLOADED"
    assert metadata["upload_authorized"] is False
    assert metadata["workflow_uploads_enabled"] is False
    workflow_text = "\n".join(path.read_text() for path in (ROOT / ".github/workflows").glob("*.yml"))
    assert "gh release upload" not in workflow_text
    assert "softprops/action-gh-release" not in workflow_text
    spec = json.loads((ROOT / "config" / "evidence-bundle.json").read_text())
    assert spec["version"] == 1 and len(spec["campaigns"]) >= 10
    assert all(not Path(item["source"]).is_absolute() for item in spec["campaigns"])
    module_spec = importlib.util.spec_from_file_location("evidence_bundle", TOOL)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory(prefix="flowcore-inventory-test-") as directory:
        root = Path(directory)
        (root / "REPORT.md").write_text("results/invalid results/interrupted\n")
        invalid = root / "results" / "invalid"
        invalid.mkdir(parents=True)
        (invalid / "summary.json").write_text(json.dumps({
            "version": 2, "status": "invalid", "runs": [{
                "valid": False, "measurement": {"state": "INVALID_INFRASTRUCTURE"},
                "offered": 3, "failed": 2, "artifact": "missing-run"}]}))
        interrupted = root / "results" / "interrupted"
        interrupted.mkdir(parents=True)
        (interrupted / "status.jsonl").write_text(
            json.dumps({"group": "/home/private/operator-name", "exit": 1, "elapsed": 1.0}) + "\n")
        inventory = module.discover_inventory(root, {
            "inventory_documents": ["REPORT.md"],
            "campaigns": [{"source": "results/invalid/summary.json",
                            "preregistered_held_out": False}]})
        assert len(inventory) == 2
        serialized = json.dumps(inventory)
        assert "/home/private/operator-name" not in serialized
        invalid_row = next(row for row in inventory if row["campaign_state"] == "invalid")
        assert invalid_row["run_classifications"]["INVALID_INFRASTRUCTURE"] == 1
        assert invalid_row["run_classifications"]["WORKLOAD_FAILURE"] == 1
        incomplete = next(row for row in inventory if row["campaign_state"] == "INTERRUPTED_OR_UNSUMMARIZED")
        assert incomplete["status_records"][0]["exit"] == 1
    unchanged = (ROOT / "README.md").read_bytes()
    refusal = subprocess.run(
        [sys.executable, "-I", str(TOOL), "build", "--root", str(ROOT),
         "--spec", str(ROOT / "config/evidence-bundle.json"),
         "--output", str(ROOT / "README.md")],
        cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert refusal.returncode != 0 and "build/evidence-candidate" in refusal.stderr
    assert (ROOT / "README.md").read_bytes() == unchanged
    print("PASS: standalone verifier, inventory classification/privacy, and local-only bundle output")


if __name__ == "__main__":
    main()
