"""Fail-closed public candidate gate. This script never publishes or chooses a license."""
import argparse
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
GATES={'functional','security','performance_evidence','reproducibility','portability','documentation','packaging','reliability','api_stability'}
STATES={'PASS','PARTIAL','FAIL','BLOCKED','UNVERIFIED'}
DOCS=('README.md','CONTRIBUTING.md','SECURITY.md','FEATURE_MATRIX.md',
      'BENCHMARK_REPORT.md','RELEASE_READINESS.md','OSS_ROADMAP.md',
      'FLOWCORE_1_0_READINESS.md','INDEPENDENT_VALIDATION.md','PERFORMANCE_EVIDENCE.md',
      'KNOWN_LIMITATIONS.md','NEXT_RESEARCH_ROADMAP.md')

def evaluate(root,data):
    if (not isinstance(data,dict) or set(data)!={'version','scope','license_approved','production_ready','gates'}
        or type(data['version']) is not int or data['version']!=1
        or type(data['license_approved']) is not bool or data['production_ready'] is not False
        or not isinstance(data['scope'],str) or not isinstance(data['gates'],dict)
        or set(data['gates'])!=GATES or any(not isinstance(value,str) or value not in STATES for value in data['gates'].values())):
        raise ValueError('invalid release gate schema')
    blockers=[key+': '+value for key,value in data['gates'].items() if value!='PASS']
    licensed=any(path.is_file() and not path.is_symlink() and path.stat().st_size>=100
                 for path in (root/'LICENSE',root/'LICENSE.md',root/'LICENSE.txt'))
    if not licensed:blockers.append('owner-selected project license file missing')
    if not data['license_approved']:blockers.append('owner license approval missing')
    for name in DOCS:
        if not (root/name).is_file():blockers.append('missing documentation: '+name)
    return dict(scope=data['scope'],publication_allowed=not blockers,
                production_ready=data['production_ready'] and not blockers,
                blockers=blockers,license_file_present=licensed)

def unique(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ValueError('duplicate gate field')
        value[key]=item
    return value

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--enforce',action='store_true')
    args=parser.parse_args()
    data=json.loads((ROOT/'config/release-gates.json').read_text(),object_pairs_hook=unique)
    result=evaluate(ROOT,data)
    print(json.dumps(result,indent=2))
    return int(args.enforce and not result['publication_allowed'])
if __name__=='__main__':raise SystemExit(main())
