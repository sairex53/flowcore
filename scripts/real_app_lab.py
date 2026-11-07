"""Reproducible real-application comparisons inside FLOWCORE-owned namespaces."""
from paths import BINDIR, RESULTS, ROOT, RUNTIME
import argparse
import csv
import hashlib
import json
import math
import os
import random
import secrets
import shutil
import subprocess
import sys
import time
import tempfile
from pathlib import Path

from benchmark import owned_directory
from cli import request
from daemon import SOCKET
from observer_perf import await_service, stop
from research_runner import exclusive
from service_config import load

APPS = ROOT / 'examples' / 'real_apps'
MODES = ('linux-default', 'fq_codel', 'cake', 'static', 'hybrid', 'predictive')


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')


def command(namespace_pid, script, args, **kwargs):
    return subprocess.Popen(['nsenter', '-t', str(namespace_pid), '-n',
                             sys.executable, str(script), *map(str, args)],
                            start_new_session=True, **kwargs)


def wait_ready(process, log, phrase, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f'service exited before ready; inspect {log}')
        try:
            if phrase in Path(log).read_text(errors='replace'):
                return
        except OSError:
            pass
        time.sleep(.05)
    raise TimeoutError(f'service startup timed out; inspect {log}')


def metadata():
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                            capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT,
                           capture_output=True, text=True, check=True).stdout
    return {'kernel': os.uname().release, 'architecture': os.uname().machine,
            'cpu_count': os.cpu_count(), 'flowcore_commit': commit,
            'flowcore_dirty': bool(dirty),
            'binary_sha256': hashlib.sha256((BINDIR / 'flowcore').read_bytes()).hexdigest(),
            'python': sys.version.split()[0]}


def proc_cpu_seconds(pid):
    raw = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    ticks = int(raw[11]) + int(raw[12])
    return ticks / os.sysconf('SC_CLK_TCK')


def launch_postgres(receiver, name, image, directory, log_stream):
    podman = shutil.which('podman')
    if not podman:
        raise RuntimeError('Podman is not installed')
    schema = APPS / 'postgres_schema.sql'
    argv = [podman, 'run', '--rm', '--name', name, '--network',
            f'ns:/proc/{receiver}/ns/net',
            '-e', 'POSTGRES_DB=postgres', '-e', 'POSTGRES_USER=postgres',
            '-e', 'POSTGRES_HOST_AUTH_METHOD=trust',
            '-v', f'{schema}:/docker-entrypoint-initdb.d/10-flowcore.sql:ro,Z',
            image]
    process = subprocess.Popen(argv, stdout=log_stream, stderr=log_stream,
                               start_new_session=True)
    phrase = 'database system is ready to accept connections'
    wait_ready(process, directory / 'postgres.log', phrase, timeout=45)
    return process


def wait_postgres(sender, process):
    probe = ("import psycopg; c=psycopg.connect('postgresql://postgres@10.203.0.2:5432/postgres', "
             "connect_timeout=2); assert c.execute('SELECT count(*) FROM flowcore_bench').fetchone()[0] == 4096; c.close()")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('PostgreSQL container exited before readiness')
        result = subprocess.run(['nsenter', '-t', str(sender), '-n', sys.executable, '-c', probe],
                                capture_output=True, text=True, timeout=4)
        if result.returncode == 0:
            return
        time.sleep(.25)
    raise TimeoutError('PostgreSQL schema did not become queryable')


def run_trial(mode, rep, seed, args, output):
    directory = output / f'{mode}-rep{rep:02d}'
    owned_directory(directory)
    cfg = load(ROOT / 'config' / 'service.json')
    cfg['intent_api'] = {'priorities': ['critical', 'interactive', 'standard']}
    cfg['lab']['bandwidth_kbit'] = args.capacity_kbit
    cfg['telemetry_seconds'] = 1
    cfg['observability']['bpf'] = 'off'
    cfg['adaptive'].update(window_seconds=2, cooldown_seconds=2, minimum_samples=20)
    cfg['policies']['default']['classes']['critical']['min_pct'] = args.critical_min_pct
    cfg['policies']['default']['classes']['interactive']['port'] = 9002
    cfg['policies']['default']['classes']['standard']['port'] = 5432
    cfg['policies']['default']['classes']['background']['min_pct'] = 30
    cfg['policies']['default']['classes']['critical']['priority'] = 0
    cfg['policies']['default']['classes']['interactive']['priority'] = 1
    cfg['policies']['default']['classes']['standard']['priority'] = 2
    cfg['policies']['default']['classes']['background']['priority'] = 3
    # Keep every class minimum feasible after selecting the critical reserve.
    cfg['policies']['default']['classes']['interactive']['min_pct'] = 20
    cfg['policies']['default']['classes']['standard']['min_pct'] = 10
    cfg['policies']['default']['classes']['background']['min_pct'] = 100 - args.critical_min_pct - 30
    if cfg['policies']['default']['classes']['background']['min_pct'] < 20:
        raise ValueError('critical reserve leaves less than the 20% background floor')
    if mode == 'cake':
        cfg['policies']['default'] = cfg['policies']['fair']
    config_path = directory / 'service.json'
    save(config_path, cfg)
    network = {'bandwidth_kbit': args.capacity_kbit, 'base_rtt_ms': 2,
               'jitter_ms': 0, 'loss_pct': 0, 'reorder_pct': 0}
    service_names = ('api-critical', 'backup-transfer', 'database-client', 'grpc-critical')
    token = secrets.token_urlsafe(32)
    daemon = http = collector = db = http_client = db_client = None
    grpc_server = grpc_client = None
    grpc_temp = None
    db_name = None
    token_read_fd = token_write_fd = None
    daemon_log = (directory / 'daemon.log').open('w')
    http_log = (directory / 'http-server.log').open('w')
    collector_log = (directory / 'metrics-bridge.log').open('w')
    db_log = (directory / 'postgres.log').open('w')
    grpc_log = (directory / 'grpc-server.log').open('w')
    try:
        daemon = subprocess.Popen([str(BINDIR / 'flowcored'), '--research',
                                   '--config', str(config_path)],
                                  stdout=daemon_log, stderr=daemon_log, start_new_session=True)
        await_service(daemon)
        request('lab_up')
        sender, receiver = request('inspect')['namespaces']
        applied = request('research_conditions', network=network, mode=mode,
                          seed=seed, link_up=True)
        before = request('observe')
        if mode in ('hybrid', 'predictive'):
            request('controller_start', strategy=mode)
        # Services in the receiver namespace accept only the lab veth endpoint.
        artifact = directory / 'backup.bin'
        random_bytes = random.Random(seed)
        with artifact.open('wb') as stream:
            remaining = args.background_bytes
            while remaining:
                block = bytes(random_bytes.randrange(256) for _ in range(min(65536, remaining)))
                stream.write(block)
                remaining -= len(block)
        env = dict(os.environ, OTEL_EXPORTER_OTLP_HEADERS_TOKEN=token,
                   FLOWCORE_SOCKET=str(SOCKET),
                   FLOWCORE_INTENT_LIBRARY=str(ROOT / 'build' / 'libflowcore_intent.so'))
        http = command(receiver, APPS / 'http_service.py',
                       ['--bind', '10.203.0.2', '--critical-port', 9000,
                        '--background-port', 9001, '--database-port', 9003,
                        '--max-workers', 64, '--artifact-dir', directory, '--flowcore'],
                       stdout=http_log, stderr=http_log, env=env)
        wait_ready(http, directory / 'http-server.log', '"ready": true')
        token_read_fd, token_write_fd = os.pipe()
        collector = command(receiver, APPS / 'metrics_bridge.py',
                            ['--bind', '10.203.0.2', '--port', 9464,
                             '--token-fd', token_read_fd,
                             *sum((['--allow-service', name] for name in service_names), [])],
                            stdout=collector_log, stderr=collector_log,
                            pass_fds=(token_read_fd,))
        os.close(token_read_fd)
        token_read_fd = None
        os.write(token_write_fd, token.encode('ascii'))
        os.close(token_write_fd)
        token_write_fd = None
        # The receiver sends no data until each bounded client export is complete.
        time.sleep(.2)
        exporter = 'http://10.203.0.2:9464/v1/metrics'
        if args.postgres:
            db_name = 'flowcore-db-' + secrets.token_hex(5)
            db = launch_postgres(receiver, db_name, args.postgres_image, directory, db_log)
            wait_postgres(sender, db)
        grpc_cpu_before = None
        grpc_env = env
        if args.grpc:
            grpc_temp = tempfile.TemporaryDirectory(prefix='grpc-generated-', dir=directory)
            generated = Path(grpc_temp.name)
            subprocess.run([sys.executable, '-m', 'grpc_tools.protoc',
                            '-I', str(APPS), '--python_out', str(generated),
                            '--grpc_python_out', str(generated), str(APPS / 'grpc_app.proto')],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            grpc_env = dict(env, FLOWCORE_GRPC_PROTO_DIR=str(generated))
            grpc_server = command(receiver, APPS / 'grpc_server.py',
                                  ['--bind', '10.203.0.2:9002', '--workers', 32],
                                  stdout=grpc_log, stderr=grpc_log, env=grpc_env)
            wait_ready(grpc_server, directory / 'grpc-server.log', '"ready": true')
            grpc_cpu_before = proc_cpu_seconds(grpc_server.pid)
        workload_start_ns = time.monotonic_ns() + 2_000_000_000
        server_cpu_before = proc_cpu_seconds(http.pid)
        http_client = command(sender, APPS / 'http_benchmark.py',
                              ['--host', '10.203.0.2', '--port', 9000, '--background-port', 9001,
                               '--rate', args.rate, '--duration', args.duration,
                               '--concurrency', args.concurrency, '--deadline-ms', args.deadline_ms,
                               '--background-bytes', args.background_bytes,
                               '--background-upload-file', artifact, '--seed', seed,
                               '--start-ns', workload_start_ns,
                               '--metrics-endpoint', exporter, '--output', directory / 'http.json'],
                              stdout=(directory / 'http-client.log').open('w'),
                              stderr=subprocess.STDOUT, env=env)
        if args.postgres:
            dsn = 'postgresql://postgres@10.203.0.2:5432/postgres'
            db_client = command(sender, APPS / 'postgres_benchmark.py',
                                ['--dsn', dsn, '--rate', args.db_rate, '--duration', args.duration,
                                 '--concurrency', args.db_concurrency, '--deadline-ms', args.db_deadline_ms,
                                 '--seed', seed, '--start-ns', workload_start_ns,
                                 '--metrics-endpoint', exporter, '--output', directory / 'postgres.json'],
                                stdout=(directory / 'postgres-client.log').open('w'),
                                stderr=subprocess.STDOUT, env=env)
        if args.grpc:
            grpc_client = command(sender, APPS / 'grpc_benchmark.py',
                                  ['--target', '10.203.0.2:9002', '--rate', args.grpc_rate,
                                   '--duration', args.duration, '--concurrency', args.grpc_concurrency,
                                   '--deadline-ms', args.grpc_deadline_ms, '--work-ms', 2,
                                   '--seed', seed, '--start-ns', workload_start_ns,
                                   '--metrics-endpoint', exporter, '--output', directory / 'grpc.json'],
                                  stdout=(directory / 'grpc-client.log').open('w'),
                                  stderr=subprocess.STDOUT, env=grpc_env)
        http_rc = http_client.wait(timeout=args.duration + 40)
        if http_rc != 0:
            raise RuntimeError(f'HTTP workload exited {http_rc}; inspect {directory / "http-client.log"}')
        if db_client:
            db_rc = db_client.wait(timeout=args.duration + 40)
            if db_rc != 0:
                raise RuntimeError(f'PostgreSQL workload exited {db_rc}; inspect {directory / "postgres-client.log"}')
        if grpc_client:
            grpc_rc = grpc_client.wait(timeout=args.duration + 40)
            if grpc_rc != 0:
                raise RuntimeError(f'gRPC workload exited {grpc_rc}; inspect {directory / "grpc-client.log"}')
        server_cpu_after = proc_cpu_seconds(http.pid)
        grpc_cpu_after = proc_cpu_seconds(grpc_server.pid) if grpc_server else None
        probe = ("import http.client; c=http.client.HTTPConnection('10.203.0.2',9000,timeout=2); "
                 "c.request('GET','/health'); r=c.getresponse(); print(r.read().decode()); c.close()")
        health = subprocess.run(['nsenter','-t',str(sender),'-n',sys.executable,'-c',probe],
                                capture_output=True,text=True,timeout=5,check=True)
        http_server_health = json.loads(health.stdout)
        after = request('observe')
        controller = request('controller_status')
        inspect = request('inspect')
        snapshot = request('research_snapshot')
        history = request('policy_history')
        cpu = None
        try:
            cpu = after['overhead']['daemon_cpu_seconds'] - before['overhead']['daemon_cpu_seconds']
        except (KeyError, TypeError):
            pass
        http_result = json.loads((directory / 'http.json').read_text())
        pg_result = json.loads((directory / 'postgres.json').read_text()) if args.postgres else None
        grpc_result = json.loads((directory / 'grpc.json').read_text()) if args.grpc else None
        summary = {'mode': mode, 'repetition': rep, 'seed': seed,
                   'status': 'MEASURED', 'configured_capacity_kbit': args.capacity_kbit,
                   'effective_shaper_capacity_kbit': applied.get('capacity_kbit'),
                   'configured_conditions': applied, 'http': http_result['summary'],
                   'background_transfer': http_result['background_transfer'],
                   'postgres': pg_result['summary'] if pg_result else None,
                   'grpc': grpc_result['summary'] if grpc_result else None,
                   'daemon_cpu_seconds': cpu,
                   'http_server_cpu_seconds': max(0, server_cpu_after-server_cpu_before),
                   'grpc_server_cpu_seconds': (max(0, grpc_cpu_after-grpc_cpu_before)
                                               if grpc_cpu_after is not None else None),
                   'controller_state': controller,
                   'policy_history': history, 'network_snapshot': snapshot,
                   'namespace_identity': inspect.get('namespaces'),
                   'otel_export': {'http': http_result['otel_export'],
                                   'postgres': pg_result.get('otel_export') if pg_result else None,
                                   'grpc': grpc_result.get('otel_export') if grpc_result else None},
                   'application_sdk': {'http': http_result.get('sdk'),
                                       'http_server': http_server_health.get('flowcore'),
                                       'postgres': pg_result.get('sdk') if pg_result else None,
                                       'grpc': grpc_result.get('sdk') if grpc_result else None},
                   'host': metadata()}
        save(directory / 'run.json', summary)
        return summary
    finally:
        for process in (http_client, db_client, grpc_client, http, grpc_server, collector):
            if process:
                stop(process)
        if db_name:
            subprocess.run(['podman', 'rm', '--force', db_name],
                           capture_output=True, timeout=15)
        stop(daemon)
        for descriptor in (token_read_fd, token_write_fd):
            if descriptor is not None:
                os.close(descriptor)
        for stream in (daemon_log, http_log, collector_log, db_log, grpc_log):
            stream.close()
        if grpc_temp is not None:
            grpc_temp.cleanup()


def aggregate(rows, output):
    fields = ('mode', 'repetition', 'seed', 'status', 'configured_capacity_kbit',
              'http_p50_ms', 'http_p95_ms', 'http_p99_ms', 'http_error_rate',
              'http_deadline_miss_rate', 'background_goodput_mbps',
              'background_completion_seconds', 'database_p99_ms', 'database_error_rate',
              'grpc_p99_ms', 'grpc_error_rate', 'grpc_deadline_miss_rate',
              'daemon_cpu_seconds', 'http_server_cpu_seconds', 'grpc_server_cpu_seconds',
              'effective_shaper_capacity_kbit')
    with (output / 'summary.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        for row in rows:
            http = row.get('http') or {}
            background = row.get('background_transfer') or {}
            db = row.get('postgres') or {}
            grpc = row.get('grpc') or {}
            writer.writerow({'mode': row.get('mode'), 'repetition': row.get('repetition'),
                'seed': row.get('seed'), 'status': row.get('status'),
                'configured_capacity_kbit': row.get('configured_capacity_kbit'),
                'effective_shaper_capacity_kbit': row.get('effective_shaper_capacity_kbit'),
                'http_p50_ms': http.get('p50_ms'), 'http_p95_ms': http.get('p95_ms'),
                'http_p99_ms': http.get('p99_ms'), 'http_error_rate': http.get('error_rate'),
                'http_deadline_miss_rate': http.get('deadline_miss_rate'),
                'background_goodput_mbps': background.get('goodput_mbps'),
                'background_completion_seconds': background.get('completion_seconds'),
                'database_p99_ms': db.get('p99_ms'), 'database_error_rate': db.get('error_rate'),
                'grpc_p99_ms': grpc.get('p99_ms'), 'grpc_error_rate': grpc.get('error_rate'),
                'grpc_deadline_miss_rate': grpc.get('deadline_miss_rate'),
                'daemon_cpu_seconds': row.get('daemon_cpu_seconds'),
                'http_server_cpu_seconds': row.get('http_server_cpu_seconds'),
                'grpc_server_cpu_seconds': row.get('grpc_server_cpu_seconds')})
    grouped = {}
    for row in rows:
        if row.get('status') == 'MEASURED':
            grouped.setdefault(row['mode'], []).append(row)
    html = ['<!doctype html><html><head><meta charset="utf-8"><title>FLOWCORE application lab</title>',
            '<style>body{font:15px system-ui;margin:2rem}table{border-collapse:collapse}td,th{padding:.45rem .7rem;border:1px solid #ccc;text-align:right}th:first-child,td:first-child{text-align:left}</style>',
            '</head><body><h1>FLOWCORE real application lab</h1>',
            '<p>Raw runs are retained as JSON. One run is an exploratory observation, not an independent-sample confidence interval.</p>',
            '<table><thead><tr><th>Mode</th><th>Runs</th><th>HTTP P99 (ms)</th><th>HTTP failures</th><th>Deadline misses</th><th>Background goodput (Mbps)</th><th>DB P99 (ms)</th><th>DB errors</th><th>gRPC P99 (ms)</th><th>gRPC errors</th></tr></thead><tbody>']
    for mode, runs in sorted(grouped.items()):
        http_p99 = [r['http']['p99_ms'] for r in runs if r.get('http') and r['http']['p99_ms'] is not None]
        errs = [r['http']['error_rate'] for r in runs if r.get('http')]
        misses = [r['http']['deadline_miss_rate'] for r in runs if r.get('http')]
        bg = [r['background_transfer']['goodput_mbps'] for r in runs
              if r.get('background_transfer', {}).get('goodput_mbps') is not None]
        dbp = [r['postgres']['p99_ms'] for r in runs if r.get('postgres') and r['postgres']['p99_ms'] is not None]
        dbe = [r['postgres']['error_rate'] for r in runs if r.get('postgres')]
        gp = [r['grpc']['p99_ms'] for r in runs if r.get('grpc') and r['grpc']['p99_ms'] is not None]
        ge = [r['grpc']['error_rate'] for r in runs if r.get('grpc')]
        avg = lambda seq: sum(seq) / len(seq) if seq else None
        html.append('<tr>' + ''.join(f'<td>{value}</td>' for value in (
            mode, len(runs), f'{avg(http_p99):.3f}' if http_p99 else 'n/a',
            f'{avg(errs):.4f}' if errs else 'n/a', f'{avg(misses):.4f}' if misses else 'n/a',
            f'{avg(bg):.3f}' if bg else 'n/a', f'{avg(dbp):.3f}' if dbp else 'n/a',
            f'{avg(dbe):.4f}' if dbe else 'n/a', f'{avg(gp):.3f}' if gp else 'n/a',
            f'{avg(ge):.4f}' if ge else 'n/a')) + '</tr>')
    html.append('</tbody></table></body></html>')
    (output / 'report.html').write_text('\n'.join(html) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--modes', default=','.join(MODES))
    parser.add_argument('--repetitions', type=int, default=10)
    parser.add_argument('--seed', type=int, default=47031)
    parser.add_argument('--capacity-kbit', type=int, default=1500)
    parser.add_argument('--critical-min-pct', type=int, default=40)
    parser.add_argument('--rate', type=float, default=60)
    parser.add_argument('--duration', type=int, default=15)
    parser.add_argument('--concurrency', type=int, default=8)
    parser.add_argument('--deadline-ms', type=float, default=30)
    parser.add_argument('--background-bytes', type=int, default=2 * 1024 * 1024)
    parser.add_argument('--db-rate', type=float, default=20)
    parser.add_argument('--db-concurrency', type=int, default=4)
    parser.add_argument('--db-deadline-ms', type=float, default=100)
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--postgres-image', default='docker.io/library/postgres:16-alpine')
    parser.add_argument('--grpc', action='store_true')
    parser.add_argument('--grpc-rate', type=float, default=20)
    parser.add_argument('--grpc-concurrency', type=int, default=4)
    parser.add_argument('--grpc-deadline-ms', type=float, default=30)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    selected = args.modes.split(',')
    if args.grpc:
        try:
            import grpc  # noqa: F401
            import grpc_tools  # noqa: F401
        except ImportError:
            parser.error('install examples/real_apps/requirements.txt for the gRPC lab')
    if args.postgres:
        try:
            import psycopg  # noqa: F401
        except ImportError:
            parser.error('install examples/real_apps/requirements.txt for PostgreSQL workloads')
    if args.grpc and (
            not math.isfinite(args.grpc_rate) or not .1 <= args.grpc_rate <= 500 or
            not 1 <= args.grpc_concurrency <= 64 or
            args.grpc_concurrency > max(1, math.ceil(args.grpc_rate * args.duration)) or
            not 0 < args.grpc_deadline_ms <= 600000):
        parser.error('invalid gRPC workload parameters')
    if (not math.isfinite(args.rate) or not .1 <= args.rate <= 500 or
            not math.isfinite(args.deadline_ms) or not 0 < args.deadline_ms <= 600000 or
            not 1 <= args.concurrency <= 64 or
            args.concurrency > max(1, math.ceil(args.rate * args.duration)) or
            not 1 <= args.background_bytes <= 64 * 1024 * 1024):
        parser.error('invalid HTTP workload parameters')
    if args.postgres and (
            not math.isfinite(args.db_rate) or not .1 <= args.db_rate <= 500 or
            not 1 <= args.db_concurrency <= 64 or
            args.db_concurrency > max(1, math.ceil(args.db_rate * args.duration)) or
            not math.isfinite(args.db_deadline_ms) or not 0 < args.db_deadline_ms <= 600000):
        parser.error('invalid PostgreSQL workload parameters')
    if (not selected or any(mode not in MODES for mode in selected) or len(set(selected)) != len(selected)
            or not 1 <= args.repetitions <= 30 or not 1 <= args.capacity_kbit <= 1000000
            or not 20 <= args.critical_min_pct <= 50 or not 1 <= args.duration <= 180
            or not 1 <= args.concurrency <= 64):
        parser.error('invalid experiment matrix')
    if os.geteuid() != 0:
        parser.error('isolated application experiments require the privileged research runner')
    RESULTS.mkdir(exist_ok=True)
    output = args.output or RESULTS / f'real-applications-{time.time_ns()}'
    owned_directory(output)
    trials = [(mode, repetition, (args.seed + repetition) % (2**32 - 1) + 1)
              for repetition in range(args.repetitions) for mode in selected]
    random.Random(args.seed).shuffle(trials)
    rows = []
    metadata_path = output / 'environment.json'
    save(metadata_path, {'host': metadata(), 'mode_order': trials,
                         'workload': vars(args) | {'output': str(output)},
                         'scope': 'owned two-namespace veth laboratory'})
    with exclusive():
        for mode, repetition, seed in trials:
            print(f'[{len(rows)+1}/{len(trials)}] {mode} repetition={repetition} seed={seed}', flush=True)
            try:
                result = run_trial(mode, repetition, seed, args, output)
            except Exception as error:
                result = {'mode': mode, 'repetition': repetition, 'seed': seed,
                          'status': 'INVALID_EXPERIMENT', 'error': type(error).__name__,
                          'message': str(error)[:400]}
            rows.append(result)
            save(output / 'runs.json', rows)
            aggregate(rows, output)
            print(json.dumps({'mode': mode, 'status': result['status'],
                              'http_p99_ms': result.get('http', {}).get('p99_ms'),
                              'error': result.get('message')}, allow_nan=False), flush=True)
    save(output / 'results.json', rows)
    print(json.dumps({'output': str(output), 'runs': len(rows),
                      'measured': sum(row.get('status') == 'MEASURED' for row in rows),
                      'invalid': sum(row.get('status') != 'MEASURED' for row in rows)}))
    return 0 if all(row.get('status') == 'MEASURED' for row in rows) else 2


if __name__ == '__main__':
    raise SystemExit(main())
