#!/usr/bin/env python3
"""Create a deterministic source/build/artifact provenance record without private paths."""
import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_REQUIREMENTS = ("gcc-c++", "cmake", "ninja-build", "python3", "openssl-devel", "rpm-build")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(*argv):
    try:
        return subprocess.check_output(argv, text=True, stderr=subprocess.STDOUT).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def installed_package(name):
    return command("rpm", "-q", "--qf", "%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}", name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-archive", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reproducibility-report", type=Path)
    parser.add_argument("--signing-status", choices=("unsigned", "official", "test-key"), default="unsigned")
    args = parser.parse_args()
    commit = command("git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SystemExit("cannot identify the full source commit")
    dirty = command("git", "-C", str(ROOT), "status", "--porcelain")
    if dirty:
        raise SystemExit("provenance requires a clean tracked and untracked checkout")
    source = args.source_archive.resolve()
    if not source.is_file():
        raise SystemExit("source archive is missing")
    source_digest = digest(source)
    source_epoch = int(os.environ.get(
        "SOURCE_DATE_EPOCH",
        command("git", "-C", str(ROOT), "log", "-1", "--format=%ct"),
    ))
    if command("git", "-C", str(ROOT), "log", "-1", "--format=%ct") != str(source_epoch):
        raise SystemExit("SOURCE_DATE_EPOCH does not match the source commit")
    artifacts = []
    all_paths = [source, *[path.resolve() for path in args.artifacts]]
    if len({path.name for path in all_paths}) != len(all_paths):
        raise SystemExit("artifact filenames must be unique in provenance")
    for path in all_paths:
        if not path.is_file():
            raise SystemExit(f"artifact is missing: {path.name}")
        artifacts.append({"filename": path.name, "size_bytes": path.stat().st_size,
                          "sha256": digest(path)})
    sbom = next((path for path in all_paths if path.suffix == ".json" and "spdx" in path.name.lower()), None)
    if sbom:
        document = json.loads(sbom.read_text())
        if document.get("spdxVersion") != "SPDX-2.3":
            raise SystemExit("the supplied SBOM is not SPDX-2.3")
    repro = None
    if args.reproducibility_report:
        repro_path = args.reproducibility_report.resolve()
        repro = json.loads(repro_path.read_text())
        if repro.get("status") not in ("PASS", "MISMATCH", "NOT_RUN"):
            raise SystemExit("invalid reproducibility report state")
    build_packages = {name: installed_package(name) for name in BUILD_REQUIREMENTS}
    environment = {
        "os_release": command("rpm", "-E", "%{?fedora}"),
        "architecture": platform.machine(),
        "kernel": platform.release(),
        "compiler": command("c++", "--version").splitlines()[0],
        "cmake": command("cmake", "--version").splitlines()[0],
        "ninja": command("ninja", "--version"),
        "python": command("python3", "--version"),
        "rpm": command("rpm", "--version"),
        "build_packages": build_packages,
    }
    result = {
        "schema": 1,
        "project": "FLOWCORE",
        "source": {"git_commit": commit, "source_archive": source.name,
                   "source_date_epoch": source_epoch, "sha256": source_digest},
        "build": {
            "environment": environment,
            "commands": [
                "SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) packaging/source-archive.sh",
                "rpmbuild -ba packaging/flowcore.spec --define _topdir=<fresh-topdir> "
                "--define _tmppath=<fresh-topdir>/tmp --define _buildhost flowcore-reproducible "
                "--define 'use_source_date_epoch_as_buildtime 1' "
                "--define 'build_mtime_policy clamp_to_source_date_epoch'",
                "cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release "
                "-DCMAKE_INSTALL_PREFIX=%{_prefix} -DCMAKE_INSTALL_SYSCONFDIR=%{_sysconfdir} "
                "-DCMAKE_INSTALL_LIBDIR=%{_lib} -DFLOWCORE_BPF=OFF "
                "-DCMAKE_CXX_FLAGS='-ffile-prefix-map=$PWD=/usr/src/flowcore' (RPM %build)",
                "cmake --build build (RPM %build)",
                "ctest --test-dir build --output-on-failure (RPM %check)",
                "DESTDIR=%{buildroot} cmake --install build (RPM %install)",
            ],
            "reproducibility": repro or {"status": "NOT_RUN"},
        },
        "artifacts": sorted(artifacts, key=lambda row: row["filename"]),
        "signing": {
            "status": {
                "official": "OPERATOR_SIGNED",
                "test-key": "OFFICIAL_SIGNING_BLOCKED_TEST_KEY_ONLY",
                "unsigned": "OFFICIAL_SIGNING_BLOCKED",
            }[args.signing_status],
            "method": {
                "official": "RPM OpenPGP plus detached OpenPGP signatures using the operator-managed key.",
                "test-key": "Artifacts signed with a disposable CI-only key; not an official project signature.",
                "unsigned": "No production signing key was supplied.",
            }[args.signing_status],
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": "PASS", "commit": commit, "artifact_count": len(artifacts),
                      "signing": result["signing"]["status"], "output": args.output.name},
                     sort_keys=True))


if __name__ == "__main__":
    main()
