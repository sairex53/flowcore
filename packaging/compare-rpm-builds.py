#!/usr/bin/env python3
"""Compare package files from two independent rpmbuild topdirs by content digest."""
import argparse
import hashlib
import json
from pathlib import Path


def files(directory, pattern):
    found = {path.name: path for path in Path(directory).rglob(pattern)}
    if not found:
        raise ValueError(f"no files matching {pattern} in {directory}")
    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--first", type=Path, required=True)
    parser.add_argument("--second", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-match", action="store_true")
    args = parser.parse_args()
    first = {**files(args.first / "RPMS", "flowcore-0.2.0-3*.rpm"),
             **files(args.first / "SRPMS", "flowcore-0.2.0-3*.src.rpm")}
    second = {**files(args.second / "RPMS", "flowcore-0.2.0-3*.rpm"),
              **files(args.second / "SRPMS", "flowcore-0.2.0-3*.src.rpm")}
    if set(first) != set(second):
        raise SystemExit("RPM/SRPM artifact sets differ between build roots")
    rows = []
    for name in sorted(first):
        left = hashlib.sha256(first[name].read_bytes()).hexdigest()
        right = hashlib.sha256(second[name].read_bytes()).hexdigest()
        rows.append(dict(filename=name, first_sha256=left, second_sha256=right,
                         identical=left == right))
    status = "PASS" if all(row["identical"] for row in rows) else "MISMATCH"
    result = dict(schema=1, status=status, scope="same commit, toolchain, architecture, independent rpmbuild topdirs",
                  artifacts=rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))
    if args.require_match and status != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
