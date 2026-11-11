"""Build RPM/SRPM with reproducible RPM metadata and optional transaction fixtures."""
import argparse
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--release", type=int, help="override the spec Release for isolated transaction fixtures")
parser.add_argument("--skip-check", action="store_true", help="skip %%check only for non-shipping transaction fixtures")
parser.add_argument("--topdir", type=Path, help="use an isolated rpmbuild output topdir")
parser.add_argument("--shared-dir", type=Path,
                    help="normalize RPM source/build paths for independent reproducibility builds")
parser.add_argument("--source-archive", type=Path, help="reuse an archive made from the clean checkout")
args = parser.parse_args()
if args.release is not None and args.release < 1:
    parser.error("--release must be a positive integer")
top = (args.topdir or ROOT / "build/rpm-deploy").resolve()
shared = (args.shared_dir or top).resolve()
for name in ("SOURCES", "tmp"):
    (top / name).mkdir(parents=True, exist_ok=True)
for name in ("BUILD", "SOURCES"):
    (shared / name).mkdir(parents=True, exist_ok=True)
env = dict(os.environ)
if "SOURCE_DATE_EPOCH" not in env:
    env["SOURCE_DATE_EPOCH"] = subprocess.check_output(
        ["git", "log", "-1", "--format=%ct"], cwd=ROOT, text=True
    ).strip()
if args.source_archive:
    archive = args.source_archive.resolve()
    if not archive.is_file():
        raise SystemExit(f"source archive not found: {archive}")
else:
    archive = Path(subprocess.check_output(
        [ROOT / "packaging/source-archive.sh"], cwd=ROOT, env=env, text=True
    ).strip())
    if not archive.is_absolute():
        archive = ROOT / archive
shutil.copy2(archive, top / "SOURCES")
if (shared / "SOURCES") != (top / "SOURCES"):
    shutil.copy2(archive, shared / "SOURCES")
tool = shutil.which("rpmbuild")
extra = []
if not tool:
    local = ROOT / "tools/rpm/root"
    tool = local / "usr/bin/rpmbuild"
    if not tool.is_file():
        raise SystemExit("install rpm-build or unpack it under tools/rpm/root")
    extra = [
        "--define", f"_rpmconfigdir {local}/usr/lib/rpm",
        "--define", f"__rpmuncompress {local}/usr/bin/rpmuncompress",
    ]
    # RPM 6 loads file attributes from its startup macro path, before --define.
    local_attrs = sorted((local / "usr/lib/rpm/fileattrs").glob("*.attr"))
    if not local_attrs:
        raise SystemExit("local RPM file attributes missing")
    for attr in local_attrs:
        extra.extend(["--load", str(attr)])

command = [
    str(tool), "-ba",
    "--define", f"_topdir {top}",
    "--define", f"_builddir {shared}/BUILD",
    "--define", f"_sourcedir {shared}/SOURCES",
    "--define", f"_tmppath {top}/tmp",
    "--define", "_buildhost flowcore-reproducible",
    "--define", "use_source_date_epoch_as_buildtime 1",
    "--define", "build_mtime_policy clamp_to_source_date_epoch",
]
if args.release is not None:
    command.extend(["--define", f"flowcore_release {args.release}"])
if args.skip_check:
    command.append("--nocheck")
command.extend([*extra, str(ROOT / "packaging/flowcore.spec")])
subprocess.run(command, cwd=ROOT, check=True, env=env)
