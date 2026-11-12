"""Bounded FLOWCORE runtime soak runner for owned namespace test suites.

The runner never accepts a shell command. Profiles are fixed to repository
integration tests and the Research Lab's namespace-owned benchmark command.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / 'results'
DEFAULT_MANIFEST = ROOT / 'scenarios/overload-gradual-smoke.json'
PROFILE_STEPS = {
    'research': ('research',),
    'service': ('service',),
    'udp': ('udp',),
    'scale': ('scale',),
    'full': ('research', 'service', 'udp', 'scale'),
}
MAX_NAMESPACE_SCAN = 64
MAX_INVENTORY_FILES = 100_000


def parse_proc_stat(text):
    """Parse fields needed from /proc/PID/stat, including names with spaces."""
    left = text.find('(')
    right = text.rfind(')')
    if left < 0 or right <= left:
        raise ValueError('invalid proc stat')
    comm = text[left + 1:right]
    fields = text[right + 1:].split()
    if len(fields) < 20:
        raise ValueError('short proc stat')
    return {
        'comm': comm,
        'state': fields[0],
        'ppid': int(fields[1]),
        'pgrp': int(fields[2]),
        'session': int(fields[3]),
        'cpu_ticks': int(fields[11]) + int(fields[12]),
        'start_ticks': int(fields[19]),
    }


def process_table(proc_root=Path('/proc')):
    rows = {}
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return rows
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            row = parse_proc_stat((entry / 'stat').read_text())
            status = (entry / 'status').read_text().splitlines()
            for line in status:
                if line.startswith('VmRSS:'):
                    row['rss_bytes'] = int(line.split()[1]) * 1024
                elif line.startswith('Threads:'):
                    row['threads'] = int(line.split()[1])
            row.setdefault('rss_bytes', 0)
            row.setdefault('threads', 0)
            try:
                row['open_fds'] = len(list((entry / 'fd').iterdir()))
                row['fd_status'] = 'available'
            except OSError:
                row['open_fds'] = None
                row['fd_status'] = 'unavailable'
            row['pid'] = int(entry.name)
            rows[row['pid']] = row
        except (OSError, ValueError, IndexError):
            continue
    return rows


def descendant_pids(root_pid, rows):
    if root_pid not in rows:
        return []
    children = {}
    for pid, row in rows.items():
        children.setdefault(row['ppid'], []).append(pid)
    found = []
    pending = [root_pid]
    seen = set()
    while pending:
        pid = pending.pop()
        if pid in seen or pid not in rows:
            continue
        seen.add(pid)
        found.append(pid)
        pending.extend(children.get(pid, ()))
    return sorted(found)


def process_metrics(root_pid=None, rows=None):
    rows = process_table() if rows is None else rows
    if root_pid is None:
        pids = [os.getpid()] if os.getpid() in rows else []
    else:
        pids = descendant_pids(root_pid, rows)
    selected = [rows[pid] for pid in pids]
    return {
        'process_count': len(selected),
        'rss_bytes': sum(row['rss_bytes'] for row in selected),
        'open_fds': (sum(row['open_fds'] for row in selected)
                     if all(row['open_fds'] is not None for row in selected) else None),
        'threads': sum(row['threads'] for row in selected),
        'cpu_ticks': sum(row['cpu_ticks'] for row in selected),
        'processes': [dict(pid=row['pid'], comm=row['comm'], start_ticks=row['start_ticks'],
                           rss_bytes=row['rss_bytes'], open_fds=row['open_fds'],
                           threads=row['threads'], cpu_ticks=row['cpu_ticks'])
                      for row in selected],
    }


def network_namespaces(rows):
    representatives = {}
    inaccessible = 0
    for pid in rows:
        try:
            inode = Path(f'/proc/{pid}/ns/net').stat().st_ino
        except OSError:
            inaccessible += 1
            continue
        representatives.setdefault(inode, (pid, None))
    named = {}
    netns_dir = Path('/run/netns')
    if netns_dir.is_dir():
        try:
            for path in netns_dir.iterdir():
                try:
                    inode = path.stat().st_ino
                    name_digest = hashlib.sha256(path.name.encode()).hexdigest()
                    named[name_digest] = inode
                    representatives.setdefault(inode, (None, path.name))
                except OSError:
                    inaccessible += 1
        except OSError:
            inaccessible += 1
    values = sorted((inode, holder[0], holder[1])
                    for inode, holder in representatives.items())
    return values, inaccessible, named


def _json_command(argv, timeout=2):
    try:
        result = subprocess.run(argv, check=True, capture_output=True, text=True,
                                timeout=timeout)
        value = json.loads(result.stdout)
        return value, None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        return None, type(error).__name__


def qdisc_inventory(namespaces):
    tc = shutil.which(os.environ.get('FLOWCORE_TC', 'tc'))
    nsenter = shutil.which('nsenter')
    ip = shutil.which('ip')
    if not tc or not nsenter or not ip:
        return dict(available=False, reason='tc or nsenter unavailable')
    selected = namespaces[:MAX_NAMESPACE_SCAN]
    current_inode = Path('/proc/self/ns/net').stat().st_ino
    count = 0
    errors = 0
    by_namespace = {}
    for inode, pid, name in selected:
        if inode == current_inode:
            argv = [tc, '-j', 'qdisc', 'show']
        elif name:
            argv = [ip, 'netns', 'exec', name, tc, '-j', 'qdisc', 'show']
        else:
            argv = [nsenter, '-t', str(pid), '-n', tc, '-j', 'qdisc', 'show']
        value, error = _json_command(argv, timeout=3)
        if error or not isinstance(value, list):
            errors += 1
            continue
        count += len(value)
        encoded = json.dumps(_stable_network_value(value), sort_keys=True,
                             separators=(',', ':')).encode()
        by_namespace[str(inode)] = dict(pid=pid, count=len(value),
                                        sha256=hashlib.sha256(encoded).hexdigest())
    return dict(available=errors == 0, count=count, by_namespace=by_namespace,
                current_namespace=str(current_inode),
                namespaces_scanned=len(selected), namespaces_total=len(namespaces),
                truncated=len(namespaces) > len(selected), errors=errors,
                reason='namespace scan incomplete' if errors else None)


def bpf_inventory():
    bpftool = shutil.which('bpftool')
    if not bpftool:
        return dict(available=False, reason='bpftool unavailable')
    counts = {}
    for kind, key in (('prog', 'programs'), ('map', 'maps'), ('link', 'links')):
        value, error = _json_command([bpftool, '-j', kind, 'show'], timeout=4)
        if error or not isinstance(value, list):
            return dict(available=False, reason=f'{key} inventory unavailable')
        counts[key] = len(value)
    pinned = 0
    try:
        for _root, dirs, files in os.walk('/sys/fs/bpf', followlinks=False):
            pinned += len(dirs) + len(files)
    except OSError:
        return dict(available=False, reason='bpffs inventory unavailable')
    counts['pinned_paths'] = pinned
    return dict(available=True, **counts)


def container_inventory():
    docker = shutil.which('docker')
    if not docker:
        return dict(available=False, reason='docker unavailable')
    try:
        result = subprocess.run([docker, 'volume', 'ls', '-q'], check=True,
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return dict(available=False, reason='docker volume inventory unavailable')
    return dict(available=True, volume_count=len([line for line in result.stdout.splitlines() if line]))


def _stable_network_value(value):
    dynamic = {'bytes', 'packets', 'drops', 'overlimits', 'requeues', 'lastuse',
               'stats', 'stats2', 'stats64', 'expires', 'preferred_life_time',
               'valid_life_time', 'cache', 'lastused'}
    if isinstance(value, dict):
        return {key: _stable_network_value(item) for key, item in value.items()
                if key not in dynamic}
    if isinstance(value, list):
        return [_stable_network_value(item) for item in value]
    return value


def directory_inventory(path, max_files=MAX_INVENTORY_FILES):
    path = Path(path)
    count = size = 0
    truncated = False
    if not path.exists():
        return dict(available=True, files=0, bytes=0, truncated=False)
    try:
        stack = [path]
        while stack:
            current = stack.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            stat = entry.stat(follow_symlinks=False)
                            count += 1
                            size += stat.st_size
                            if count >= max_files:
                                truncated = True
                                stack.clear()
                                break
                    except OSError:
                        continue
        return dict(available=True, files=count, bytes=size, truncated=truncated)
    except OSError as error:
        return dict(available=False, reason=type(error).__name__, files=count, bytes=size,
                    truncated=truncated)


def _host_network_signature():
    ip = shutil.which('ip')
    tc = shutil.which(os.environ.get('FLOWCORE_TC', 'tc'))
    if not ip or not tc:
        return dict(available=False, reason='ip or tc unavailable')
    commands = ([ip, '-j', 'link', 'show'], [ip, '-j', 'address', 'show'],
                [ip, '-4', '-j', 'route', 'show', 'table', 'all'],
                [ip, '-6', '-j', 'route', 'show', 'table', 'all'],
                [tc, '-j', 'qdisc', 'show'])
    rows = []
    for argv in commands:
        value, error = _json_command(argv)
        if error is not None:
            return dict(available=False, reason='host network snapshot incomplete')
        rows.append(value)
    encoded = json.dumps(_stable_network_value(rows), sort_keys=True,
                         separators=(',', ':')).encode()
    return dict(available=True, sha256=hashlib.sha256(encoded).hexdigest())


def resource_snapshot(root_pid=None, output_dir=None):
    rows = process_table()
    namespaces, inaccessible, named = network_namespaces(rows)
    try:
        mem_available = next(int(line.split()[1]) * 1024 for line in
                             Path('/proc/meminfo').read_text().splitlines()
                             if line.startswith('MemAvailable:'))
    except (OSError, StopIteration, ValueError):
        mem_available = None
    try:
        disk_free = shutil.disk_usage(output_dir or ROOT).free
    except OSError:
        disk_free = None
    try:
        tmp_entries = len(list(Path(tempfile.gettempdir()).iterdir()))
        tmp_status = 'available'
    except OSError:
        tmp_entries = None
        tmp_status = 'unavailable'
    return {
        'monotonic_ns': time.monotonic_ns(),
        'process': process_metrics(root_pid, rows),
        'memory_available_bytes': mem_available,
        'network_namespaces': dict(available=inaccessible == 0, count=len(namespaces),
                                   process_namespaces=len({inode for inode, pid, name in namespaces
                                                           if pid is not None}),
                                   named_count=len(named),
                                   named_ids=sorted(named),
                                   inaccessible_processes=inaccessible),
        'qdiscs': qdisc_inventory(namespaces),
        'bpf': bpf_inventory(),
        'container_volumes': container_inventory(),
        'temporary_directory': dict(available=tmp_status == 'available', entries=tmp_entries),
        'output_directory': directory_inventory(output_dir) if output_dir else None,
        'disk_free_bytes': disk_free,
        'host_network': _host_network_signature(),
    }


def _profile_command(profile, manifest, mode, max_minutes, build_dir):
    python = sys.executable
    if profile == 'research':
        binary = build_dir / 'flowcore'
        return [str(binary), 'bench', 'soak', str(manifest), '--cycles', '1',
                '--mode', mode, '--max-minutes', str(max_minutes)]
    tests = {
        'service': ROOT / 'tests/service_integration.py',
        'udp': ROOT / 'tests/workload_integration.py',
        'scale': ROOT / 'tests/release_scale.py',
    }
    return [python, '-B', str(tests[profile])]


def validate_args(args):
    if args.profile not in PROFILE_STEPS:
        raise ValueError('unknown runtime soak profile')
    if type(args.cycles) is not int or not 1 <= args.cycles <= 10_000:
        raise ValueError('cycles must be in [1, 10000]')
    if type(args.duration_seconds) is not int or not 1 <= args.duration_seconds <= 604_800:
        raise ValueError('duration must be in [1, 604800] seconds')
    if not 1 <= args.sample_seconds <= 300:
        raise ValueError('sample interval must be in [1, 300] seconds')
    if not 1 <= args.max_output_mb <= 65_536:
        raise ValueError('max output size must be in [1, 65536] MiB')
    if not 0 <= args.max_rss_growth_mb <= 16_384:
        raise ValueError('RSS growth limit must be in [0, 16384] MiB')
    if not 0 <= args.max_fd_growth <= 65_536 or not 0 <= args.max_thread_growth <= 65_536:
        raise ValueError('FD and thread growth limits must be in [0, 65536]')
    if not 0 <= args.min_free_mb <= 1_048_576:
        raise ValueError('minimum free disk space must be in [0, 1048576] MiB')
    if not 0 <= args.max_netns_growth <= 64 or not 0 <= args.max_qdisc_growth <= 4096:
        raise ValueError('namespace/qdisc growth limits exceed the supported bounds')
    if not 0 <= args.max_bpf_growth <= 4096 or not 0 <= args.max_volume_growth <= 4096:
        raise ValueError('BPF/volume growth limits exceed the supported bounds')
    if args.max_minutes < 1 or args.max_minutes > 1440:
        raise ValueError('per-trial limit must be in [1, 1440] minutes')
    if args.profile in ('research', 'full'):
        if args.manifest.is_symlink():
            raise ValueError('research manifest must not be a symlink')
        manifest = args.manifest.resolve(strict=True)
        scenarios = (ROOT / 'scenarios').resolve()
        if scenarios not in manifest.parents:
            raise ValueError('research manifest must be a file under scenarios/')
        if not manifest.is_file():
            raise ValueError('research manifest must be a file')
        sys.path.insert(0, str(ROOT / 'scripts'))
        from research_manifest import load_manifest
        loaded = load_manifest(manifest)
        if args.mode not in loaded['modes']:
            raise ValueError('research mode is not present in the selected manifest')
        args.manifest = manifest
    args.build_dir = args.build_dir.resolve(strict=True)
    if not args.build_dir.is_dir():
        raise ValueError('build directory must exist')
    args.tc = args.tc.resolve(strict=True)
    if not args.tc.is_file() or not os.access(args.tc, os.X_OK):
        raise ValueError('tc executable must exist and be executable')
    return args


def _terminate_group(proc, grace=5):
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=grace)


def _write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n')


def _git_metadata():
    def command(*argv):
        try:
            result = subprocess.run(argv, cwd=ROOT, check=True, capture_output=True,
                                    text=True, timeout=3)
            return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
    status = command('git', 'status', '--porcelain')
    return dict(commit=command('git', 'rev-parse', 'HEAD'), dirty=bool(status))


def _research_artifacts(previous):
    artifacts = []
    for path in sorted(RESULTS.glob('soak-*/soak.json')):
        resolved = path.resolve()
        if resolved in previous or RESULTS.resolve() not in resolved.parents or path.is_symlink():
            continue
        try:
            soak = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        trials = []
        for cycle in soak.get('runs', []):
            parent = Path(cycle.get('artifact', ''))
            summary_path = parent / 'summary.json'
            try:
                summary = json.loads(summary_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            for run in summary.get('runs', []):
                trials.append({key: run.get(key) for key in (
                    'mode', 'valid', 'offered', 'completed', 'failed', 'deadline_misses',
                    'deadline_attainment', 'p99_ms', 'background_mbps', 'total_mbps',
                'cpu_seconds', 'adjustments', 'rollbacks', 'jain_index', 'error')})
        artifacts.append(dict(path=str(path.relative_to(ROOT)), outcome=soak.get('outcome'),
                              cycles_completed=soak.get('completed_cycles'), trials=trials))
    return artifacts


def _check_delta(name, before, after, limit):
    if not before.get('available') or not after.get('available'):
        return dict(status='UNVERIFIED', reason='inventory unavailable')
    delta = after.get(name) - before.get(name)
    return dict(status='PASS' if delta <= limit else 'FAIL', delta=delta, limit=limit)


def _resource_gates(before, after, limits, leaked):
    gates = {'process_cleanup': dict(status='PASS' if not leaked else 'FAIL',
                                    leaked_processes=leaked)}
    gates['network_namespaces'] = _check_delta(
        'count', before['network_namespaces'], after['network_namespaces'], limits['netns'])
    temp_before, temp_after = before['temporary_directory'], after['temporary_directory']
    if temp_before.get('available') and temp_after.get('available'):
        temp_delta = temp_after['entries'] - temp_before['entries']
        gates['temporary_files'] = dict(status='PASS' if temp_delta == 0 else 'UNVERIFIED',
            entries_delta=temp_delta,
            reason=None if temp_delta == 0 else
            'global temporary directory changed; ownership cannot be attributed safely')
    else:
        gates['temporary_files'] = dict(status='UNVERIFIED', reason='temporary directory inventory unavailable')
    qdisc_before, qdisc_after = before['qdiscs'], after['qdiscs']
    qdisc_delta = (qdisc_after.get('count', 0) - qdisc_before.get('count', 0)
                   if qdisc_before.get('available') and qdisc_after.get('available') else None)
    if qdisc_delta is None:
        gates['qdiscs'] = dict(status='UNVERIFIED', reason='qdisc inventory incomplete')
    elif qdisc_delta <= limits['qdiscs']:
        gates['qdiscs'] = dict(status='PASS', delta=qdisc_delta, limit=limits['qdiscs'])
    else:
        before_ns = qdisc_before.get('by_namespace', {})
        after_ns = qdisc_after.get('by_namespace', {})
        changed = sorted(key for key in before_ns.keys() & after_ns.keys()
                         if before_ns[key].get('sha256') != after_ns[key].get('sha256'))
        stable_set = before_ns.keys() == after_ns.keys()
        current = qdisc_before.get('current_namespace')
        host_changed = stable_set and current in changed
        gates['qdiscs'] = dict(status='FAIL' if host_changed else 'UNVERIFIED',
                               delta=qdisc_delta, limit=limits['qdiscs'],
                               changed_namespaces=changed, namespace_set_stable=stable_set,
                               reason=('qdisc state changed in host network namespace' if host_changed
                                       else 'qdisc total changed outside a stable host namespace; attribution is inconclusive'))
    gates['container_volumes'] = _check_delta(
        'volume_count', before['container_volumes'], after['container_volumes'], limits['volumes'])
    if before['container_volumes'].get('available'):
        before['container_volumes']['volume_count'] = before['container_volumes'].get('volume_count')
    bpf_before, bpf_after = before['bpf'], after['bpf']
    if bpf_before.get('available') and bpf_after.get('available'):
        deltas = {key: bpf_after[key] - bpf_before[key]
                  for key in ('programs', 'maps', 'links', 'pinned_paths')}
        gates['bpf_resources'] = dict(status='PASS' if max(deltas.values(), default=0) <= limits['bpf']
                                      else 'FAIL', deltas=deltas, limit=limits['bpf'])
    else:
        gates['bpf_resources'] = dict(status='UNVERIFIED',
                                      reason=bpf_after.get('reason') or bpf_before.get('reason'))
    left, right = before['host_network'], after['host_network']
    if left.get('available') and right.get('available'):
        same = left['sha256'] == right['sha256']
        gates['host_network_unchanged'] = dict(status='PASS' if same else 'FAIL', unchanged=same)
    else:
        gates['host_network_unchanged'] = dict(status='UNVERIFIED', reason='network inventory incomplete')
    return gates


def _numeric_delta(first, last):
    if not isinstance(first, (int, float)) or not isinstance(last, (int, float)):
        return None
    delta = last - first
    return dict(first=first, last=last, absolute_delta=delta,
                relative_delta_percent=(100.0 * delta / first if first else None))


def _stability_summary(steps, limits):
    by_profile = {}
    for step in steps:
        if step.get('peak_process'):
            by_profile.setdefault(step['profile'], []).append(step)
    resources = {}
    for profile, rows in by_profile.items():
        rows.sort(key=lambda row: row.get('cycle', 0))
        first, last = rows[0]['peak_process'], rows[-1]['peak_process']
        comparisons = {name: _numeric_delta(first.get(name), last.get(name))
                       for name in ('rss_bytes', 'open_fds', 'threads')}
        checks = []
        for name, bound in (('rss_bytes', limits['rss_growth_bytes']),
                            ('open_fds', limits['fd_growth']),
                            ('threads', limits['thread_growth'])):
            delta = comparisons[name]
            if delta is not None:
                checks.append(delta['absolute_delta'] <= bound)
        resources[profile] = dict(samples=len(rows), first_cycle=rows[0].get('cycle'),
                                  last_cycle=rows[-1].get('cycle'), comparisons=comparisons,
                                  limits=limits,
                                  status=('UNVERIFIED' if len(rows) < 2 or not checks else
                                          'PASS' if all(checks) else 'FAIL'))

    research_trials = []
    for step in steps:
        for artifact in step.get('research_artifacts', []):
            research_trials.extend(artifact.get('trials', []))
    modes = {}
    for trial in research_trials:
        if not trial.get('mode'):
            continue
        modes.setdefault(trial['mode'], []).append(trial)
    performance = {}
    for mode, rows in modes.items():
        rows.sort(key=lambda row: row.get('trial_order', 0))
        valid_rows = [row for row in rows if row.get('valid') is True]
        first = valid_rows[0] if valid_rows else {}
        last = valid_rows[-1] if valid_rows else {}
        first_failure_rate = (first['failed'] / first['offered']
                              if isinstance(first.get('failed'), (int, float))
                              and isinstance(first.get('offered'), (int, float))
                              and first['offered'] > 0 else None)
        last_failure_rate = (last['failed'] / last['offered']
                             if isinstance(last.get('failed'), (int, float))
                             and isinstance(last.get('offered'), (int, float))
                             and last['offered'] > 0 else None)
        performance[mode] = dict(trials=len(rows), valid_trials=len(valid_rows),
            failed_requests=sum(row.get('failed', 0) or 0 for row in rows),
            offered_requests=sum(row.get('offered', 0) or 0 for row in rows),
            controller_adjustments=sum(row.get('adjustments', 0) or 0 for row in rows),
            controller_rollbacks=sum(row.get('rollbacks', 0) or 0 for row in rows),
            early_to_late=dict(p99_ms=_numeric_delta(first.get('p99_ms'), last.get('p99_ms')),
                               failure_rate=_numeric_delta(first_failure_rate, last_failure_rate),
                               background_mbps=_numeric_delta(first.get('background_mbps'),
                                                              last.get('background_mbps')),
                               cpu_seconds=_numeric_delta(first.get('cpu_seconds'),
                                                          last.get('cpu_seconds'))),
            interpretation='descriptive early-to-late observations; not a paired regression test')
    statuses = [row['status'] for row in resources.values()]
    drift_status = ('FAIL' if 'FAIL' in statuses else 'PASS'
                    if statuses and all(value == 'PASS' for value in statuses) else 'UNVERIFIED')
    return dict(resource_by_profile=resources, workload_by_mode=performance,
                resource_drift_status=drift_status)


def completed_cycle_count(steps, expected_profiles):
    expected = set(expected_profiles)
    by_cycle = {}
    for step in steps:
        cycle = step.get('cycle')
        if type(cycle) is not int:
            continue
        row = by_cycle.setdefault(cycle, dict(profiles=[], passed=True))
        row['profiles'].append(step.get('profile'))
        row['passed'] = row['passed'] and step.get('status') == 'PASS'
    return sum(1 for row in by_cycle.values()
               if row['passed'] and len(row['profiles']) == len(expected)
               and set(row['profiles']) == expected)


def _leaked_processes(observed, rows=None):
    rows = process_table() if rows is None else rows
    leaked = []
    for pid, start_ticks in observed.items():
        row = rows.get(pid)
        if row and row['start_ticks'] == start_ticks:
            leaked.append(dict(pid=pid, comm=row['comm']))
    return leaked


def _run_step(profile, argv, output_dir, log_path, deadline, sample_seconds,
              max_output_bytes, samples, environment=None, min_free_bytes=0):
    started = time.monotonic()
    seen = {}
    previous_research = {path.resolve() for path in RESULTS.glob('soak-*/soak.json')}
    peak = dict(rss_bytes=0, open_fds=0, threads=0, process_count=0, cpu_ticks=0)
    timed_out = False
    output_limited = False
    disk_limited = False
    with log_path.open('wb') as log:
        try:
            proc = subprocess.Popen(argv, cwd=ROOT, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True, env=environment)
        except OSError as error:
            return dict(profile=profile, argv=argv, status='BLOCKED',
                        error=type(error).__name__, elapsed_seconds=time.monotonic()-started,
                        log=str(log_path.relative_to(ROOT)))
        while proc.poll() is None:
            now = time.monotonic()
            snapshot = resource_snapshot(proc.pid, output_dir)
            samples.append(snapshot)
            proc_rows = snapshot['process']['processes']
            for row in proc_rows:
                seen[row['pid']] = row['start_ticks']
            for key in peak:
                value = snapshot['process'].get(key)
                if value is not None:
                    peak[key] = max(peak[key], value)
            output_size = snapshot['output_directory']['bytes'] if snapshot['output_directory'] else 0
            free_bytes = snapshot.get('disk_free_bytes')
            if free_bytes is not None and free_bytes < min_free_bytes:
                disk_limited = True
                _terminate_group(proc)
                break
            if output_size > max_output_bytes:
                output_limited = True
                _terminate_group(proc)
                break
            if now >= deadline:
                timed_out = True
                _terminate_group(proc)
                break
            time.sleep(min(sample_seconds, max(0.05, deadline-now)))
        returncode = proc.wait()
        log.flush()
        os.fsync(log.fileno())
    time.sleep(0.1)
    leaked = _leaked_processes(seen)
    if disk_limited:
        status = 'STOPPED_DISK_GUARD'
    elif output_limited:
        status = 'STOPPED_OUTPUT_GUARD'
    elif timed_out:
        status = 'TIME_LIMIT'
    elif leaked:
        status = 'FAIL_PROCESS_LEAK'
    else:
        status = 'PASS' if returncode == 0 else 'FAIL'
    research = _research_artifacts(previous_research) if profile == 'research' else []
    return dict(profile=profile, argv=argv, status=status, returncode=returncode,
                elapsed_seconds=round(time.monotonic()-started, 3), peak_process=peak,
                leaked_processes=leaked, log=str(log_path.relative_to(ROOT)),
                research_artifacts=research)


def run_soak(args):
    if os.geteuid() != 0:
        raise PermissionError('runtime soak profiles require root for owned network namespaces')
    if (ROOT / 'installed-layout').exists():
        raise PermissionError('run the source-tree soak only from a disposable development checkout')
    RESULTS.mkdir(mode=0o700, exist_ok=True)
    output_dir = RESULTS / f'runtime-soak-{time.time_ns()}'
    output_dir.mkdir(mode=0o700)
    os.environ['FLOWCORE_TC'] = str(args.tc)
    steps = PROFILE_STEPS[args.profile]
    start = time.monotonic()
    duration_target = start + args.duration_seconds
    limits = dict(netns=args.max_netns_growth, qdiscs=args.max_qdisc_growth,
                  bpf=args.max_bpf_growth, volumes=args.max_volume_growth,
                  rss_growth_bytes=args.max_rss_growth_mb*1024*1024,
                  fd_growth=args.max_fd_growth, thread_growth=args.max_thread_growth,
                  disk_free_minimum_bytes=args.min_free_mb*1024*1024)
    initial = resource_snapshot(None, output_dir)
    min_free_bytes = limits['disk_free_minimum_bytes']
    _write_json(output_dir / 'metadata.json', dict(
        schema_version=1, profile=args.profile, cycles_requested=args.cycles,
        duration_target_seconds=args.duration_seconds, sample_interval_seconds=args.sample_seconds,
        mode=args.mode if args.profile in ('research', 'full') else None,
        manifest=str(args.manifest) if args.profile in ('research', 'full') else None,
        architecture=os.uname().machine, kernel=os.uname().release, git=_git_metadata(),
        limits=limits, command_profiles=list(steps), created_unix_ns=time.time_ns()))
    samples = [initial]
    completed = []
    stop_reason = None
    if initial['disk_free_bytes'] is not None and initial['disk_free_bytes'] < min_free_bytes:
        stop_reason = 'BLOCKED_LOW_DISK_SPACE'
    for cycle in range(args.cycles):
        if stop_reason:
            break
        if time.monotonic() >= duration_target:
            stop_reason = 'DURATION_REACHED'
            break
        for profile in steps:
            argv = _profile_command(profile, args.manifest, args.mode, args.max_minutes,
                                    args.build_dir)
            if profile == 'research' and not Path(argv[0]).is_file():
                step = dict(profile=profile, argv=argv, status='BLOCKED',
                            error='build/flowcore is missing', elapsed_seconds=0)
            else:
                log = output_dir / f'cycle-{cycle:04d}-{profile}.log'
                environment = os.environ.copy()
                environment['FLOWCORE_BUILD_DIR'] = str(args.build_dir)
                environment['FLOWCORE_TC'] = str(args.tc)
                step_deadline = time.monotonic() + args.max_minutes * 60
                step = _run_step(profile, argv, output_dir, log, step_deadline,
                                 args.sample_seconds, args.max_output_mb*1024*1024, samples,
                                 environment, min_free_bytes)
            step['cycle'] = cycle
            completed.append(step)
            _write_json(output_dir / 'progress.json', dict(
                status='running', completed_steps=len(completed),
                completed_cycles=completed_cycle_count(completed, steps),
                latest_step={key: step.get(key) for key in
                             ('cycle', 'profile', 'status', 'returncode', 'elapsed_seconds')},
                latest_sample=samples[-1]))
            if step['status'] != 'PASS':
                stop_reason = step['status']
                break
        if stop_reason:
            break
        if time.monotonic() >= duration_target:
            stop_reason = 'DURATION_REACHED'
            break
    final = resource_snapshot(None, output_dir)
    samples.append(final)
    with (output_dir / 'samples.jsonl').open('w') as stream:
        for row in samples:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + '\n')
    leaked = sorted({(item['pid'], item['comm']) for step in completed
                     for item in step.get('leaked_processes', [])})
    gates = _resource_gates(initial, final, limits, [dict(pid=pid, comm=comm)
                                                     for pid, comm in leaked])
    if final['disk_free_bytes'] is None:
        gates['disk_space'] = dict(status='UNVERIFIED', reason='free disk space unavailable')
    else:
        gates['disk_space'] = dict(status='PASS' if final['disk_free_bytes'] >= min_free_bytes else 'BLOCKED',
                                   free_bytes=final['disk_free_bytes'], minimum_bytes=min_free_bytes)
    output_bytes = final['output_directory']['bytes'] if final['output_directory'] else 0
    if output_bytes > args.max_output_mb*1024*1024:
        gates['output_size'] = dict(status='FAIL', bytes=output_bytes,
                                    limit_bytes=args.max_output_mb*1024*1024)
    else:
        gates['output_size'] = dict(status='PASS', bytes=output_bytes,
                                    limit_bytes=args.max_output_mb*1024*1024)
    completed_cycles = completed_cycle_count(completed, steps)
    observed_cycles = len({row['cycle'] for row in completed})
    all_steps_pass = bool(completed) and completed_cycles == observed_cycles
    if not stop_reason:
        stop_reason = ('DURATION_REACHED' if time.monotonic()-start >= args.duration_seconds
                       else 'CYCLE_LIMIT_REACHED')
    failed_gates = [name for name, gate in gates.items() if gate['status'] == 'FAIL']
    blocked_gates = [name for name, gate in gates.items() if gate['status'] == 'BLOCKED']
    if failed_gates or any(row['status'].startswith('FAIL') or row['status'] == 'STOPPED_OUTPUT_GUARD'
                           for row in completed):
        outcome = 'FAIL'
    elif blocked_gates or any(row['status'] == 'STOPPED_DISK_GUARD' for row in completed):
        outcome = 'BLOCKED'
    elif all_steps_pass and stop_reason in ('DURATION_REACHED', 'CYCLE_LIMIT_REACHED'):
        outcome = 'PASS'
    elif stop_reason:
        outcome = 'BLOCKED' if stop_reason in ('BLOCKED_LOW_DISK_SPACE', 'STOPPED_DISK_GUARD') else stop_reason
    else:
        outcome = 'INCOMPLETE'
    stability = _stability_summary(completed, limits)
    gates['resource_drift'] = dict(status=stability['resource_drift_status'],
                                   profiles=stability['resource_by_profile'])
    if gates['resource_drift']['status'] == 'FAIL':
        failed_gates.append('resource_drift')
        outcome = 'FAIL'
    elapsed_seconds = round(time.monotonic()-start, 3)
    report = dict(schema_version=1, status=outcome, decision='PASS' if outcome == 'PASS' else 'INCOMPLETE',
                  profile=args.profile, steps=completed, cycles_completed=(
                      completed_cycles),
                  cycles_requested=args.cycles, duration_target_seconds=args.duration_seconds,
                  duration_target_reached=elapsed_seconds >= args.duration_seconds,
                  elapsed_seconds=elapsed_seconds,
                  samples=len(samples), baseline=initial, final=final,
                  regression=dict(resource_gates=gates, failed_gates=failed_gates,
                                  blocked_gates=blocked_gates,
                                  early_late=stability),
                  limits=limits, stop_reason=stop_reason,
                  host_network_unchanged=gates['host_network_unchanged']['status'] == 'PASS')
    _write_json(output_dir / 'report.json', report)
    print(json.dumps(dict(status=outcome, report=str(output_dir/'report.json'),
                          steps=len(completed), cycles_completed=report['cycles_completed']),
                     sort_keys=True))
    return 0 if outcome == 'PASS' else (2 if outcome in ('BLOCKED', 'TIME_LIMIT', 'INCOMPLETE') else 1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=sorted(PROFILE_STEPS), default='full')
    parser.add_argument('--manifest', type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument('--build-dir', type=Path, default=ROOT / 'build')
    parser.add_argument('--tc', type=Path,
                        default=Path(shutil.which('tc') or ROOT/'tools/root/usr/bin/tc'))
    parser.add_argument('--mode', default='optimizer-overload')
    parser.add_argument('--cycles', type=int, default=1)
    parser.add_argument('--duration-seconds', type=int, default=900,
                        help='target wall time; finish the current complete profile cycle')
    parser.add_argument('--sample-seconds', type=int, default=5)
    parser.add_argument('--max-minutes', type=float, default=15,
                        help='bound each inner Research Lab trial')
    parser.add_argument('--max-output-mb', type=int, default=4096)
    parser.add_argument('--max-netns-growth', type=int, default=0)
    parser.add_argument('--max-qdisc-growth', type=int, default=0)
    parser.add_argument('--max-bpf-growth', type=int, default=0)
    parser.add_argument('--max-volume-growth', type=int, default=0)
    parser.add_argument('--max-rss-growth-mb', type=int, default=128)
    parser.add_argument('--max-fd-growth', type=int, default=32)
    parser.add_argument('--max-thread-growth', type=int, default=16)
    parser.add_argument('--min-free-mb', type=int, default=1024)
    args = parser.parse_args(argv)
    try:
        validate_args(args)
        return run_soak(args)
    except (ValueError, PermissionError, OSError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    raise SystemExit(main())
