"""Check local Markdown link targets without making external network requests."""
import re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
errors=[];checked=0
for path in sorted(ROOT.glob('*.md')):
    for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)',path.read_text()):
        target=target.strip('<>').split('#',1)[0]
        if not target or re.match(r'^[a-zA-Z]+:',target) or target.startswith('/'):
            continue
        checked+=1
        if not (path.parent/target).exists():errors.append(f'{path.name}: {target}')
if errors:raise SystemExit('\n'.join(errors))
print(f'PASS: {checked} local Markdown links')
