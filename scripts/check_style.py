"""Check source hygiene without changing existing project formatting."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUFFIXES = {'.py','.cpp','.hpp','.c','.h','.json','.yml','.sh'}
paths = [p for name in ('scripts','src','include','tests','examples','config','scenarios','bpf','.github')
         for p in (ROOT/name).rglob('*') if p.is_file() and p.suffix in SUFFIXES]
errors = []
for path in sorted(paths):
    data = path.read_bytes()
    for line, text in enumerate(data.splitlines(),1):
        if text.rstrip(b' \t') != text:
            errors.append(f'{path.relative_to(ROOT)}:{line}: trailing whitespace')
    if data and not data.endswith(b'\n'):
        errors.append(f'{path.relative_to(ROOT)}: missing final newline')
    if b'\r' in data:
        errors.append(f'{path.relative_to(ROOT)}: CR in source')
if errors:
    raise SystemExit('\n'.join(errors))
print(f'Checked source formatting hygiene in {len(paths)} files')
