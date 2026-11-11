#!/usr/bin/env python3
"""Fail closed if the FLOWCORE C ABI v2 loses symbols or changes its ELF major."""
import argparse
import json
import re
import subprocess
from pathlib import Path


def output(*argv):
    return subprocess.check_output(argv, text=True, stderr=subprocess.STDOUT)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--soname", default="libflowcore_intent.so.2")
    parser.add_argument("--version-node", default="FLOWCORE_INTENT_2")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    baseline = {
        line.strip() for line in args.baseline.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if not baseline or any(not re.fullmatch(r"fc_v2_[A-Za-z0-9_]+", row) for row in baseline):
        raise SystemExit("invalid checked-in C ABI symbol baseline")

    symbols = set()
    raw = output("nm", "-D", "--defined-only", "--format=posix", str(args.library))
    for line in raw.splitlines():
        fields = line.split()
        if not fields:
            continue
        name = fields[0].split("@", 1)[0]
        if name.startswith("fc_v2_"):
            symbols.add(name)
    missing = sorted(baseline - symbols)
    if missing:
        raise SystemExit("removed C ABI v2 symbols: " + ", ".join(missing))

    leaked = sorted(name for name in symbols if not re.fullmatch(r"fc_v2_[A-Za-z0-9_]+", name))
    unexpected_exports = []
    for line in raw.splitlines():
        fields = line.split()
        if not fields:
            continue
        name = fields[0].split("@", 1)[0]
        if name == args.version_node or name.startswith("fc_v2_"):
            continue
        if fields[-1] not in ("U", "u"):
            unexpected_exports.append(name)
    if leaked or unexpected_exports:
        raise SystemExit("unexpected exported symbols: " + ", ".join(sorted(set(leaked + unexpected_exports))))

    unversioned = sorted(
        line.split()[0].split("@", 1)[0] for line in raw.splitlines()
        if line.split() and line.split()[0].startswith("fc_v2_") and "@@" not in line.split()[0]
    )
    if unversioned:
        raise SystemExit("unversioned public symbols: " + ", ".join(unversioned))

    dynamic = output("readelf", "-d", str(args.library))
    soname_match = re.search(r"\(SONAME\).*\[([^]]+)\]", dynamic)
    if not soname_match or soname_match.group(1) != args.soname:
        raise SystemExit(f"expected SONAME {args.soname}, got {soname_match.group(1) if soname_match else 'none'}")
    version_info = output("readelf", "--version-info", str(args.library))
    if args.version_node not in version_info:
        raise SystemExit(f"missing ELF symbol version node {args.version_node}")

    report = dict(status="PASS", soname=args.soname, version_node=args.version_node,
                  baseline_symbols=len(baseline), exported_symbols=len(symbols),
                  additive_symbols=sorted(symbols - baseline))
    if args.report:
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
