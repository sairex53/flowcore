"""Build RPM/SRPM with normal dependency generation, including local RPM 6 tools."""
import os
import shutil
import subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
top=ROOT/'build/rpm-deploy'
for name in ('SOURCES','tmp'): (top/name).mkdir(parents=True,exist_ok=True)
env=dict(os.environ)
env.setdefault('SOURCE_DATE_EPOCH',subprocess.check_output(['git','log','-1','--format=%ct'],cwd=ROOT,text=True).strip())
archive=subprocess.check_output([ROOT/'packaging/source-archive.sh'],cwd=ROOT,env=env,text=True).strip()
shutil.copy2(ROOT/archive,top/'SOURCES')
tool=shutil.which('rpmbuild')
extra=[]
if not tool:
    local=ROOT/'tools/rpm/root'
    tool=local/'usr/bin/rpmbuild'
    if not tool.is_file():raise SystemExit('install rpm-build or unpack it under tools/rpm/root')
    extra=['--define',f'_rpmconfigdir {local}/usr/lib/rpm',
           '--define',f'__rpmuncompress {local}/usr/bin/rpmuncompress']
    # RPM 6 loads file attributes from its startup macro path, before --define.
    # Explicitly load the relocated attributes; otherwise ELF dependencies vanish.
    attributes=sorted((local/'usr/lib/rpm/fileattrs').glob('*.attr'))
    if not attributes:raise SystemExit('local RPM file attributes missing')
    for path in attributes:extra.extend(['--load',str(path)])
subprocess.run([str(tool),'-ba','--define',f'_topdir {top}','--define',f'_tmppath {top}/tmp',
                *extra,ROOT/'packaging/flowcore.spec'],cwd=ROOT,check=True,env=env)
