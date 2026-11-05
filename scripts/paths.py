"""Source-tree and installed layout. Writable state never lives in installed code."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLED = (ROOT/'installed-layout').is_file()
BINDIR = (ROOT/(ROOT/'installed-layout').read_text().strip()).resolve() if INSTALLED else ROOT/'build'
BPF_DATA = ROOT/'bpf' if INSTALLED else ROOT/'build/bpf'
BPF_MARKER = ROOT/'bpf-enabled' if INSTALLED else ROOT/'build/bpf-enabled'
RUNTIME = Path(os.environ.get('FLOWCORE_RUNTIME', '/run/flowcore' if INSTALLED else str(ROOT/'runtime')))
RESULTS = Path(os.environ.get('FLOWCORE_RESULTS', '/var/lib/flowcore/results' if INSTALLED else str(ROOT/'results')))
STATE = Path(os.environ.get('FLOWCORE_STATE',str(RUNTIME)))
SOCKET = RUNTIME/'control.sock'
TC = Path(os.environ.get('FLOWCORE_TC','/usr/bin/tc' if INSTALLED else str(ROOT/'tools/root/usr/bin/tc')))

_version = ROOT/'VERSION' if INSTALLED else BINDIR/'VERSION'
VERSION = _version.read_text().strip() if _version.is_file() else '0.2.0'
