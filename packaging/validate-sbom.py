#!/usr/bin/env python3
"""Offline structural and digest validator for a FLOWCORE SPDX evidence bundle."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "generate_sbom", Path(__file__).with_name("generate-sbom.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
validate = module.validate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sbom", type=Path)
    parser.add_argument("--rpm", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--schema", type=Path)
    args = parser.parse_args()
    document = json.loads(args.sbom.read_text())
    validate(document)
    if args.schema:
        import jsonschema
        schema = json.loads(args.schema.read_text())
        jsonschema.Draft7Validator.check_schema(schema)
        jsonschema.validate(document, schema)
    by_name = {row["name"]: row for row in document["packages"]}
    for name, path in (("flowcore", args.rpm), ("flowcore-source", args.source)):
        if path is None:
            continue
        row = by_name[name]
        expected = {entry["checksumValue"] for entry in row.get("checksums", [])
                    if entry.get("algorithm") == "SHA256"}
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected != {actual}:
            raise SystemExit(f"{name} checksum does not match the SPDX package record")
    print(f"PASS: SPDX 2.3 structure and requested SHA-256 checks ({args.sbom.name})")


if __name__ == "__main__":
    main()
