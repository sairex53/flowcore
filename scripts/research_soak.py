"""Optional isolated repeated-run soak with bounded duration and resource guards."""
import copy
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from research_manifest import MODES, validate
from research_runner import RESULTS, execute, owned_directory, save
from research_report import report as render_report


def bpf_resources():
    binary = shutil.which('bpftool')
    if not binary:
        return dict(available=False,reason='bpftool unavailable')
    try:
        counts = {}
        for kind,name in (('prog','programs'),('map','maps'),('link','links')):
            result = subprocess.run([binary,'-j',kind,'show'],check=True,capture_output=True,
                                    text=True,timeout=3)
            value = json.loads(result.stdout)
            if not isinstance(value,list):
                raise ValueError(f'unexpected bpftool {kind} response')
            counts[name] = len(value)
        pinned = 0
        for _root,dirs,files in os.walk('/sys/fs/bpf',followlinks=False):
            pinned += len(dirs)+len(files)
        counts['pinned_paths'] = pinned
        return dict(available=True,**counts)
    except (OSError,subprocess.SubprocessError,ValueError,json.JSONDecodeError) as error:
        return dict(available=False,reason=type(error).__name__)


def resources():
    status = Path('/proc/self/status').read_text().splitlines()
    rss = next(int(line.split()[1])*1024 for line in status if line.startswith('VmRSS:'))
    return dict(rss_bytes=rss, open_fds=len(list(Path('/proc/self/fd').iterdir())),
                host_netns=os.stat('/proc/self/ns/net').st_ino,bpf=bpf_resources())


def run_soak(manifest, cycles, mode, max_minutes=60, max_rss_growth_mb=256, max_fd_growth=32,
             max_bpf_growth=8):
    manifest = validate(manifest)
    if mode not in manifest['modes']:
        raise ValueError('soak mode must exist in the manifest comparison matrix')
    if type(cycles) is not int or not 1 <= cycles <= 1000:
        raise ValueError('cycles must be in [1, 1000]')
    if type(max_minutes) not in (int,float) or not 1 <= max_minutes <= 1440:
        raise ValueError('max_minutes must be in [1, 1440]')
    if type(max_rss_growth_mb) not in (int,float) or not 0 <= max_rss_growth_mb <= 16384:
        raise ValueError('max_rss_growth_mb must be in [0, 16384]')
    if type(max_fd_growth) is not int or not 0 <= max_fd_growth <= 65536:
        raise ValueError('max_fd_growth must be in [0, 65536]')
    if type(max_bpf_growth) is not int or not 0 <= max_bpf_growth <= 4096:
        raise ValueError('max_bpf_growth must be in [0, 4096]')
    directory = RESULTS/f'soak-{time.time_ns()}'
    owned_directory(directory)
    base = resources()
    deadline = time.monotonic()+max_minutes*60
    rows = []
    for cycle in range(cycles):
        if time.monotonic() >= deadline:
            break
        candidate = copy.deepcopy(manifest)
        candidate['modes'] = [mode]
        candidate['repetitions'] = 1
        candidate['seed'] = (manifest['seed']+cycle)%(2**32-1)
        before = resources()
        output,summary = execute(candidate,label=f'cycle-{cycle:05d}',parent=directory)
        render_report(output/'summary.json')
        after = resources()
        report = summary['runs'][0]
        artifact = output/('r0-'+mode)
        network = json.loads((artifact/'network.json').read_text()) if (artifact/'network.json').is_file() else {}
        stale_holders = [pid for pid in network.get('namespaces',[])
                         if type(pid) is int and Path(f'/proc/{pid}/ns/net').exists()]
        row = dict(cycle=cycle, seed=candidate['seed'], valid=report.get('valid',False),
                   error=report.get('error'), p99_ms=report.get('p99_ms'), failed=report.get('failed'),
                   before=before, after=after, stale_namespace_holder_pids=stale_holders,
                   artifact=str(output))
        rows.append(row)
        with (directory/'soak.jsonl').open('a') as stream:
            stream.write(json.dumps(row,allow_nan=False)+'\n')
            stream.flush()
        if after['host_netns'] != base['host_netns']:
            row['outcome'] = 'INVALID_HOST_NAMESPACE_CHANGED'
            break
        if after['rss_bytes']-base['rss_bytes'] > max_rss_growth_mb*1024*1024:
            row['outcome'] = 'STOPPED_RSS_GUARD'
            break
        if after['open_fds']-base['open_fds'] > max_fd_growth:
            row['outcome'] = 'STOPPED_FD_GUARD'
            break
        if base['bpf'].get('available') and after['bpf'].get('available'):
            growth = max(after['bpf'][key]-base['bpf'][key]
                         for key in ('programs','maps','links','pinned_paths'))
            if growth > max_bpf_growth:
                row['outcome'] = 'STOPPED_BPF_RESOURCE_GUARD'
                row['bpf_growth'] = growth
                break
        if stale_holders:
            row['outcome'] = 'STOPPED_NAMESPACE_LEAK'
            break
        if not report.get('valid'):
            row['outcome'] = 'STOPPED_INVALID_TRIAL'
            break
    outcome = rows[-1].get('outcome','COMPLETE' if len(rows)==cycles else 'TIME_LIMIT') if rows else 'TIME_LIMIT'
    save(directory/'soak.json',dict(outcome=outcome,requested_cycles=cycles,completed_cycles=len(rows),
         elapsed_seconds=time.monotonic()-(deadline-max_minutes*60),baseline=base,limits=dict(
             max_minutes=max_minutes,max_rss_growth_mb=max_rss_growth_mb,max_fd_growth=max_fd_growth,
             max_bpf_growth=max_bpf_growth),runs=rows))
    return dict(directory=str(directory),outcome=outcome,requested_cycles=cycles,completed_cycles=len(rows),
                elapsed_seconds=time.monotonic()-(deadline-max_minutes*60),baseline=base)
