"""Root-only RPM file transactions in a disposable project-local database.
Scriptlets/dependency resolution are deliberately not claimed by this test.
"""
import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--old-rpm',type=Path,required=True)
    parser.add_argument('--new-rpm',type=Path,required=True)
    args=parser.parse_args()
    if os.geteuid()!=0:raise PermissionError('isolated RPM database needs root')
    with tempfile.TemporaryDirectory(dir=ROOT/'build',prefix='rpm-cycle-') as temp:
        root=Path(temp)
        (root/'var/lib/rpm').mkdir(parents=True)
        def rpm(*arguments,check=True):
            result=subprocess.run(['rpm','--root',str(root),'--dbpath','/var/lib/rpm',*map(str,arguments)],
                                  capture_output=True,text=True,timeout=30)
            if check and result.returncode:raise RuntimeError(result.stderr+result.stdout)
            return result
        rpm('--initdb')
        rpm('-i','--nodeps','--noscripts',args.old_rpm.absolute())
        assert rpm('-V','--nodeps','flowcore').stdout==''
        config=root/'etc/flowcore/service.json';modified=config.read_bytes()+b'\n'
        config.write_bytes(modified)
        state=root/'var/lib/flowcore/state';state.mkdir(parents=True)
        marker=state/'operator-marker';marker.write_text('preserve')
        rpm('-U','--nodeps','--noscripts',args.new_rpm.absolute())
        assert config.read_bytes()==modified
        version=rpm('-q','--queryformat','%{VERSION}-%{RELEASE}','flowcore').stdout
        assert version.startswith('0.2.0-2')
        integrity=rpm('-V','--nodeps','flowcore',check=False)
        assert all('/etc/flowcore/service.json' in line for line in integrity.stdout.splitlines())
        rpm('-e','--nodeps','--noscripts','flowcore')
        assert not (root/'usr/bin/flowcore').exists()
        assert marker.read_text()=='preserve'
        saved=config.with_suffix('.json.rpmsave')
        assert saved.read_bytes()==modified
        print(json.dumps(dict(status='PASS',upgraded_to=version,modified_config_preserved=True,
            package_integrity='only intentional config edit',erase_preserved_state=True,
            rpm_scripts_executed=False,host_database_modified=False)))
if __name__=='__main__':main()
