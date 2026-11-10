"""Reproducible real-application comparisons inside FLOWCORE-owned namespaces."""
from paths import BINDIR, RESULTS, ROOT
import argparse
import csv
import hashlib
import html
import json
import math
import os
import random
import re
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
from kernel_api_experiment import host_identity
from research_stats import median_ci
import statistics
from importlib.metadata import PackageNotFoundError, version
from research_runner import exclusive
from service_config import load

APPS = ROOT / 'examples' / 'real_apps'
MODES = ('linux-default', 'fq_codel', 'cake', 'static', 'hybrid', 'predictive')
CONTROLLERS = ('hybrid', 'predictive', 'slo-aware', 'queue-aware')
PINNED_POSTGRES = ('docker.io/library/postgres@sha256:'
                   '9f5be6795d4e9dfc0dca4c8a46eb858a9ddef631fc6b57953960f578d3fb1f9b')


def dependencies():
    values = {}
    for package in ('grpcio', 'grpcio-tools', 'protobuf', 'psycopg', 'psycopg-binary'):
        try:
            values[package] = version(package)
        except PackageNotFoundError:
            pass
    return values


def rss_bytes(pid):
    fields = dict(line.split(':',1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
    return int(fields['VmRSS'].split()[0])*1024 if 'VmRSS' in fields else None


def stages(profile, duration, network):
    if profile != 'dynamic':
        return []
    reduced = dict(network, bandwidth_kbit=max(100, network['bandwidth_kbit']//2),
                   base_rtt_ms=16, loss_pct=.5)
    return [(duration*.25, 'capacity_rtt_loss', reduced, True),
            (duration*.50, 'interrupt', reduced, False),
            (duration*.65, 'restore', network, True)]


def recovery(rows, timeline, deadline_ms):
    restored = next((entry['confirmed_ns'] for entry in timeline if entry['phase']=='restore'), None)
    if restored is None:
        return None
    on_time = [row['end_ns'] for row in rows if row['scheduled_ns'] >= restored and row['outcome']=='ok'
               and row['end_ns']-row['scheduled_ns'] <= deadline_ms*1e6]
    return (min(on_time)-restored)/1e6 if on_time else None



def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')


def command(namespace, script, args, *, interpreter=sys.executable, **kwargs):
    return subprocess.Popen(['nsenter', '--net='+namespace, '--',
                             str(interpreter), str(script), *map(str, args)],
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
            'python': sys.version.split()[0], 'dependencies': dependencies()}


def proc_cpu_seconds(pid):
    raw = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    ticks = int(raw[11]) + int(raw[12])
    return ticks / os.sysconf('SC_CLK_TCK')


def launch_postgres(receiver, name, image, directory, log_stream, *, statement_stats=False):
    podman = shutil.which('podman')
    if not podman:
        raise RuntimeError('Podman is not installed')
    # Podman interprets relative mount sources as named volumes.
    schema = (directory / 'postgres-schema.sql').absolute()
    shutil.copyfile(APPS / 'postgres_schema.sql', schema)
    if statement_stats:
        with schema.open('a') as stream:
            stream.write('\nCREATE EXTENSION IF NOT EXISTS pg_stat_statements;\n')
    argv = [podman, 'run', '--pull=never', '--rm', '--name', name, '--network',
            f'ns:{receiver}',
            '-e', 'POSTGRES_DB=postgres', '-e', 'POSTGRES_USER=postgres',
            '-e', 'POSTGRES_HOST_AUTH_METHOD=trust',
            '-v', f'{schema}:/docker-entrypoint-initdb.d/10-flowcore.sql:ro,Z',
            image, *(['postgres','-c','shared_preload_libraries=pg_stat_statements'] if statement_stats else [])]
    process = subprocess.Popen(argv, stdout=log_stream, stderr=log_stream,
                               start_new_session=True)
    phrase = 'database system is ready to accept connections'
    try:
        wait_ready(process, directory / 'postgres.log', phrase, timeout=45)
        return process
    except BaseException:
        stop(process)
        raise


def wait_postgres(sender, process, host="10.203.0.2"):
    probe = (f"import psycopg; c=psycopg.connect('postgresql://postgres@{host}:5432/postgres', "
             "connect_timeout=2); assert c.execute('SELECT count(*) FROM flowcore_bench').fetchone()[0] == 4096; c.close()")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('PostgreSQL container exited before readiness')
        result = subprocess.run(['nsenter', '--net='+sender, '--', sys.executable, '-c', probe],
                                capture_output=True, text=True, timeout=4)
        if result.returncode == 0:
            return
        time.sleep(.25)
    raise TimeoutError('PostgreSQL schema did not become queryable')


def database_execution(name, duration):
    query = ("SELECT coalesce(sum(calls),0), coalesce(sum(total_exec_time),0), coalesce(sum(rows),0) "
             "FROM pg_stat_statements WHERE query LIKE 'SELECT value FROM flowcore_bench WHERE id = %'")
    raw = subprocess.check_output(['podman','exec',name,'psql','-X','-U','postgres','-At','-c',query],
                                  text=True,timeout=6).strip()
    count,elapsed,rows=raw.split('|')
    return dict(calls=int(count), executor_total_ms=float(elapsed), rows=int(rows),
                calls_per_offered_second=int(count)/duration,
                executor_mean_ms=float(elapsed)/int(count) if int(count) else None)


def cgroup_resources(pid):
    try:
        cg = next(line[3:] for line in Path(f'/proc/{pid}/cgroup').read_text().splitlines()
                  if line.startswith('0::'))
        root = Path('/sys/fs/cgroup')
        path = (root/cg.lstrip('/')).resolve()
        if path==root or not path.is_relative_to(root):
            return None
        cpu = dict(line.split() for line in (path/'cpu.stat').read_text().splitlines())
        return dict(cpu_seconds=int(cpu['usage_usec'])/1e6,
                    memory_bytes=int((path/'memory.current').read_text()),
                    memory_peak_bytes=int((path/'memory.peak').read_text()))
    except (OSError, ValueError, KeyError, StopIteration):
        return None


def stage_summary(rows, timeline, duration, start):
    phases=[('initial',start),*((event['phase'],event['confirmed_ns']) for event in timeline),
            ('end',start+int(duration*1e9))]
    report={}
    for (name,lower),(_,upper) in zip(phases,phases[1:]):
        offered=[row for row in rows if lower<=row['scheduled_ns']<upper]
        good=[row['latency_ms'] for row in offered if row['outcome']=='ok']
        report[name]=dict(offered=len(offered),successful=len(good),
                          deadline_misses=sum(row['deadline_miss'] for row in offered),
                          p99_ms=sorted(good)[math.ceil(.99*len(good))-1] if good else None)
    return report


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
    service_names = ('api-critical', 'backup-transfer', 'database-client', 'grpc-critical', 'http3-client')
    token = secrets.token_urlsafe(32)
    daemon = http = collector = db = http_client = db_client = None
    grpc_server = grpc_client = backend = bulk_service = None
    timeline = []
    namespace_fds = []
    grpc_temp = h3_temp = None
    h3_server = h3_client = None
    db_name = None
    token_read_fd = token_write_fd = None
    daemon_log = (directory / 'daemon.log').open('w')
    http_log = (directory / 'http-server.log').open('w')
    collector_log = (directory / 'metrics-bridge.log').open('w')
    db_log = (directory / 'postgres.log').open('w')
    grpc_log = (directory / 'grpc-server.log').open('w')
    backend_log = (directory / 'backend.log').open('w')
    bulk_log = (directory / 'bulk-server.log').open('w')
    h3_log = (directory / 'http3-server.log').open('w')
    try:
        trial_begin_ns = time.monotonic_ns()
        daemon_env = dict(os.environ,FLOWCORE_STATE=str(directory/'state'))
        daemon = subprocess.Popen([str(BINDIR / 'flowcored'), '--research',
                                   '--config', str(config_path)],
                                  stdout=daemon_log, stderr=daemon_log, start_new_session=True, env=daemon_env)
        await_service(daemon)
        request('lab_up')
        holders = request('inspect')['namespaces']
        host_ns = os.stat('/proc/self/ns/net')
        identities = {(host_ns.st_dev,host_ns.st_ino)}
        namespace_inodes = []
        for pid in holders:
            fd = os.open(f'/proc/{pid}/ns/net',os.O_RDONLY|os.O_CLOEXEC)
            namespace_fds.append(fd)
            identity = os.fstat(fd)
            pair = (identity.st_dev,identity.st_ino)
            if pair in identities:
                raise RuntimeError('application lab requires distinct owned namespaces')
            identities.add(pair)
            namespace_inodes.append(pair)
        sender, receiver = (f'/proc/{os.getpid()}/fd/{fd}' for fd in namespace_fds)
        applied = request('research_conditions', network=network, mode=mode,
                          seed=seed, link_up=True)
        before = request('observe')
        if mode in CONTROLLERS:
            request('controller_start', strategy=mode)
        # Services in the receiver namespace accept only the lab veth endpoint.
        artifact = directory / 'backup.bin'
        random_bytes = random.Random(seed)
        with artifact.open('wb') as stream:
            remaining = args.background_bytes
            while remaining:
                block = random_bytes.randbytes(min(65536, remaining))
                stream.write(block)
                remaining -= len(block)
        env = dict(os.environ, OTEL_EXPORTER_OTLP_HEADERS_TOKEN=token,
                   FLOWCORE_SOCKET=str(SOCKET),
                   FLOWCORE_INTENT_LIBRARY=str(ROOT / 'build' / 'libflowcore_intent.so'))
        sdk_flags = ['--no-flowcore'] if args.stack and mode == 'linux-default' else []
        server_sdk = [] if sdk_flags else ['--flowcore']
        db_host = '10.203.0.1' if args.stack else '10.203.0.2'
        if args.postgres:
            db_name = 'flowcore-db-' + secrets.token_hex(5)
            db = launch_postgres(sender if args.stack else receiver, db_name, args.postgres_image, directory, db_log, statement_stats=args.stack)
            wait_postgres(receiver if args.stack else sender, db, db_host)
        if args.stack:
            backend = command(receiver, APPS / 'http_service.py',
                              ['--bind', '127.0.0.1', '--critical-port', 9010, '--only-role', 'critical',
                               '--pool-size', args.pool_size, '--max-workers', 64,
                               '--database-dsn', f'postgresql://postgres@{db_host}:5432/postgres',
                               '--service', 'api-backend', *server_sdk],
                              stdout=backend_log, stderr=backend_log, env=env)
            wait_ready(backend, directory / 'backend.log', '"ready": true')
            bulk_service = command(receiver, APPS / 'http_service.py',
                                   ['--bind', '10.203.0.2', '--only-role', 'background',
                                    '--max-workers', 16, '--artifact-dir', directory],
                                   stdout=bulk_log, stderr=bulk_log, env=env)
            wait_ready(bulk_service, directory / 'bulk-server.log', '"ready": true')
        gateway_flags = ['--only-role', 'critical', '--upstream-port', 9010,
                         '--pool-size', args.pool_size] if args.stack else []
        http = command(receiver, APPS / 'http_service.py',
                       ['--bind', '10.203.0.2', '--critical-port', 9000,
                        '--background-port', 9001, '--database-port', 9003,
                        '--max-workers', 64, '--artifact-dir', directory, *server_sdk, *gateway_flags],
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
                                  ['--bind', '10.203.0.2:9002', '--workers', 32,
                                   *(['--upstream-port', 9010, '--pool-size', args.pool_size] if args.stack else [])],
                                  stdout=grpc_log, stderr=grpc_log, env=grpc_env)
            wait_ready(grpc_server, directory / 'grpc-server.log', '"ready": true')
            grpc_cpu_before = proc_cpu_seconds(grpc_server.pid)
        if args.http3_python:
            h3_temp = tempfile.TemporaryDirectory(prefix='app-tls-', dir=directory)
            subprocess.run([args.http3_python,'-B','-c',
                            'from lab_tls import certificate; import sys; certificate(sys.argv[1])',
                            h3_temp.name],cwd=ROOT/'scripts',check=True,timeout=10)
            cert,key = (Path(h3_temp.name)/name for name in ('cert.pem','key.pem'))
            h3_server = command(receiver, APPS/'http3_app.py',
                                ['server','--host','10.203.0.2','--certificate',cert,'--key',key,
                                 '--pool-size',args.pool_size],interpreter=args.http3_python,
                                stdout=h3_log,stderr=h3_log,env=env)
            wait_ready(h3_server, directory/'http3-server.log', '"ready": true')
        workload_start_ns = time.monotonic_ns() + 2_000_000_000
        processes = {name: process for name, process in (('daemon',daemon), ('gateway',http),
                     ('backend',backend), ('bulk',bulk_service), ('collector',collector),
                     ('grpc',grpc_server), ('http3',h3_server)) if process is not None}
        if db_name:
            pid = int(subprocess.check_output(['podman','inspect','--format','{{.State.Pid}}',db_name],
                                              text=True,timeout=5))
            db_meta = subprocess.check_output(['podman','inspect','--format','{{.Image}}',db_name],
                                              text=True,timeout=5).strip()
            save(directory/'postgres-runtime.json', dict(image=args.postgres_image, image_id=db_meta, pid=pid))
        db_before = cgroup_resources(pid) if db_name else None
        component_before = {name: proc_cpu_seconds(proc.pid) for name,proc in processes.items()}
        workload_flags = ['--work-ms', 40 if args.profile == 'application' else 2,
                          '--burst-size', 4 if args.profile == 'dynamic' else 1,
                          '--response-sizes', 128, 512, 2048, 4096] if args.stack else []
        upload_flags = ['--background-upload-file', artifact] if args.background_bytes else []

        server_cpu_before = proc_cpu_seconds(http.pid)
        http_client = command(sender, APPS / 'http_benchmark.py',
                              ['--host', '10.203.0.2', '--port', 9000, '--background-port', 9001,
                               '--rate', args.rate, '--duration', args.duration,
                               '--concurrency', args.concurrency, '--deadline-ms', args.deadline_ms,
                               '--background-bytes', args.background_bytes,
                               *upload_flags, *workload_flags, *sdk_flags, '--seed', seed,
                               '--start-ns', workload_start_ns,
                               '--metrics-endpoint', exporter, '--output', directory / 'http.json'],
                              stdout=(directory / 'http-client.log').open('w'),
                              stderr=subprocess.STDOUT, env=env)
        if args.postgres:
            dsn = f'postgresql://postgres@{db_host}:5432/postgres'
            db_client = command(receiver if args.stack else sender, APPS / 'postgres_benchmark.py',
                                ['--dsn', dsn, '--rate', args.db_rate, '--duration', args.duration,
                                 '--concurrency', args.db_concurrency, '--deadline-ms', args.db_deadline_ms, *sdk_flags,
                                 '--seed', seed, '--start-ns', workload_start_ns,
                                 '--metrics-endpoint', exporter, '--output', directory / 'postgres.json'],
                                stdout=(directory / 'postgres-client.log').open('w'),
                                stderr=subprocess.STDOUT, env=env)
        if args.grpc:
            grpc_client = command(sender, APPS / 'grpc_benchmark.py',
                                  ['--target', '10.203.0.2:9002', '--rate', args.grpc_rate,
                                   '--duration', args.duration, '--concurrency', args.grpc_concurrency,
                                   '--deadline-ms', args.grpc_deadline_ms, '--work-ms', 2, *sdk_flags,
                                   *(['--mixed', '--cancel-every', args.grpc_cancel_every] if args.stack else []),
                                   '--seed', seed, '--start-ns', workload_start_ns,
                                   '--metrics-endpoint', exporter, '--output', directory / 'grpc.json'],
                                  stdout=(directory / 'grpc-client.log').open('w'),
                                  stderr=subprocess.STDOUT, env=grpc_env)
        if h3_server:
            with (directory/'http3-client.log').open('w') as log:
                h3_client=command(sender,APPS/'http3_app.py',
                    ['client','--host','10.203.0.2','--certificate',cert,
                     '--rate',args.http3_rate,'--duration',args.duration,'--concurrency',args.http3_concurrency,
                     '--deadline-ms',args.http3_deadline_ms,'--seed',seed,'--start-ns',workload_start_ns,
                     '--metrics-endpoint',exporter,'--output',directory/'http3.json',*sdk_flags],
                    interpreter=args.http3_python,stdout=log,stderr=log,env=env)
        for offset, phase, settings, up in stages(args.profile, args.duration, network):
            scheduled = workload_start_ns+int(offset*1e9)
            time.sleep(max(0,(scheduled-time.monotonic_ns())/1e9))
            started = time.monotonic_ns()
            confirmed = request('research_conditions', network=settings, mode=mode, seed=seed, link_up=up)
            timeline.append(dict(phase=phase, scheduled_ns=scheduled, started_ns=started,
                                 confirmed_ns=confirmed['kernel_confirmed_ns'], readback=confirmed))
            save(directory/'network-timeline.json', timeline)
            if confirmed['kernel_confirmed_ns']-scheduled > 500_000_000:
                raise RuntimeError('scheduled network transition exceeded 500 ms integrity window')
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
        if h3_client and h3_client.wait(timeout=args.duration+20):
            raise RuntimeError(f'HTTP/3 workload failed; inspect {directory / "http3-client.log"}')
        components = {name: dict(cpu_seconds=max(0,proc_cpu_seconds(proc.pid)-component_before[name]),
                                 rss_bytes=rss_bytes(proc.pid)) for name,proc in processes.items()}
        db_after = cgroup_resources(pid) if db_name else None
        if db_before and db_after:
            components['postgres_container'] = dict(db_after, cpu_seconds=max(0,
                db_after['cpu_seconds']-db_before['cpu_seconds']))
        db_execution = database_execution(db_name,args.duration) if args.stack else None
        server_cpu_after = proc_cpu_seconds(http.pid)
        grpc_cpu_after = proc_cpu_seconds(grpc_server.pid) if grpc_server else None
        probe = ("import http.client; c=http.client.HTTPConnection('10.203.0.2',9000,timeout=2); "
                 "c.request('GET','/health'); r=c.getresponse(); print(r.read().decode()); c.close()")
        health = subprocess.run(['nsenter','--net='+sender,'--',sys.executable,'-c',probe],
                                capture_output=True,text=True,timeout=5,check=True)
        http_server_health = json.loads(health.stdout)
        after = request('observe')
        controller = request('controller_status')
        inspect = request('inspect')
        snapshot = request('research_snapshot')
        history = request('policy_history')
        if any(row['time_ns'] < trial_begin_ns for row in history):
            raise RuntimeError('daemon decision history contains a prior trial')
        controller_changes = sum(row['result'] in ('pending','verified') for row in history)
        cpu = None
        try:
            cpu = after['overhead']['daemon_cpu_seconds'] - before['overhead']['daemon_cpu_seconds']
        except (KeyError, TypeError):
            pass
        http_result = json.loads((directory / 'http.json').read_text())
        pg_result = json.loads((directory / 'postgres.json').read_text()) if args.postgres else None
        grpc_result = json.loads((directory / 'grpc.json').read_text()) if args.grpc else None
        h3_result = json.loads((directory / 'http3.json').read_text()) if h3_client else None
        for result, rate in ((http_result,args.rate),(pg_result,args.db_rate),
                             (grpc_result,args.grpc_rate),(h3_result,args.http3_rate)):
            if result is not None:
                verify_requests(result,math.ceil(rate*args.duration))
        timing = {}
        for key in ('gateway_pool','db_pool','db_roundtrip','app_work_configured'):
            values = [row['server_timing_ms'][key] for row in http_result['requests']
                      if row['outcome']=='ok' and key in row.get('server_timing_ms',{})]
            timing[key] = statistics.median(values) if values else None
        summary = {'mode': mode, 'repetition': rep, 'seed': seed,
                   'status': 'MEASURED', 'configured_capacity_kbit': args.capacity_kbit,
                   'effective_shaper_capacity_kbit': applied.get('capacity_kbit'),
                   'configured_conditions': applied, 'http': http_result['summary'],
                   'background_transfer': http_result['background_transfer'],
                   'postgres': pg_result['summary'] if pg_result else None,
                   'grpc': grpc_result['summary'] if grpc_result else None,
                   'http3': h3_result['summary'] if h3_result else None,
                   'http3_loss_events': h3_result['client_quic_loss_events'] if h3_result else None,
                   'http3_recovery_ms': recovery(h3_result['requests'],timeline,args.http3_deadline_ms) if h3_result else None,
                   'daemon_cpu_seconds': cpu, 'components': components,
                   'profile': args.profile, 'faults': timeline,
                   'http_phases': stage_summary(http_result['requests'], timeline, args.duration, workload_start_ns),
                   'http_server_timing_p50_ms': timing,
                   'postgres_execution': db_execution,
                   'recovery_ms': recovery(http_result['requests'], timeline, args.deadline_ms),
                   'http_server_cpu_seconds': max(0, server_cpu_after-server_cpu_before),
                   'grpc_server_cpu_seconds': (max(0, grpc_cpu_after-grpc_cpu_before)
                                               if grpc_cpu_after is not None else None),
                   'controller_state': controller, 'controller_verified_changes': controller_changes,
                   'policy_history': history, 'network_snapshot': snapshot,
                   'namespace_identity': inspect.get('namespaces'), 'namespace_inodes': namespace_inodes,
                   'otel_export': {'http': http_result['otel_export'],
                                   'postgres': pg_result.get('otel_export') if pg_result else None,
                                   'grpc': grpc_result.get('otel_export') if grpc_result else None,
                                   'http3': h3_result.get('otel_export') if h3_result else None},
                   'application_sdk': {'http': http_result.get('sdk'),
                                       'http_server': http_server_health.get('flowcore'),
                                       'postgres': pg_result.get('sdk') if pg_result else None,
                                       'grpc': grpc_result.get('sdk') if grpc_result else None,
                                       'http3': h3_result.get('sdk') if h3_result else None},
                   'host': metadata()}
        save(directory / 'run.json', summary)
        return summary
    finally:
        for process in (http_client, db_client, grpc_client, h3_client, http, grpc_server, h3_server, backend, bulk_service, collector):
            if process:
                stop(process)
        if db_name:
            subprocess.run(['podman', 'rm', '--force', db_name],
                           capture_output=True, timeout=15)
        if db is not None:
            stop(db)
        stop(daemon)
        for fd in namespace_fds:
            os.close(fd)
        for descriptor in (token_read_fd, token_write_fd):
            if descriptor is not None:
                os.close(descriptor)
        for stream in (daemon_log, http_log, collector_log, db_log, grpc_log):
            stream.close()
        if grpc_temp is not None:
            grpc_temp.cleanup()
        if h3_temp is not None:
            h3_temp.cleanup()


def flat_row(row):
    fields = {key: row.get(key) for key in (
        'mode','repetition','seed','profile','status','configured_capacity_kbit',
        'effective_shaper_capacity_kbit','daemon_cpu_seconds','http_server_cpu_seconds',
        'grpc_server_cpu_seconds','recovery_ms','http3_recovery_ms','http3_loss_events')}
    for prefix, section in (('http','http'),('database','postgres'),('grpc','grpc'),('http3','http3')):
        values = row.get(section) or {}
        for field in ('p50_ms','p95_ms','p99_ms','error_rate','deadline_miss_rate','completion_rps'):
            fields[f'{prefix}_{field}'] = values.get('query_rps' if prefix=='database' and field=='completion_rps' else field)
    background = row.get('background_transfer') or {}
    fields.update(background_goodput_mbps=background.get('goodput_mbps'),
                  background_completion_seconds=background.get('completion_seconds'),
                  postgres_executor_mean_ms=(row.get('postgres_execution') or {}).get('executor_mean_ms'))
    return fields


def aggregate(rows, output):
    flat = [flat_row(row) for row in rows]
    with (output / 'summary.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, flat[0])
        writer.writeheader()
        writer.writerows(flat)
    columns = ('http_p99_ms','http_error_rate','http_deadline_miss_rate','background_goodput_mbps',
               'database_p99_ms','database_completion_rps','grpc_p99_ms','grpc_error_rate',
               'http3_p99_ms','http3_error_rate','http3_loss_events','recovery_ms','daemon_cpu_seconds')
    head = ['<!doctype html><meta charset="utf-8"><title>FLOWCORE application lab</title>',
            '<h1>FLOWCORE application results</h1>',
            '<p>Medians across measured runs. P99 uses successful requests. See paired.json and raw attempts.</p>',
            '<table><thead><tr><th>Mode</th><th>Measured / total</th>' +
            ''.join(f'<th>{html.escape(field)}</th>' for field in columns)+'</tr></thead><tbody>']
    for mode in sorted({row['mode'] for row in flat}):
        all_rows = [row for row in flat if row['mode']==mode]
        measured = [row for row in all_rows if row['status']=='MEASURED']
        cells = []
        for field in columns:
            values = [row[field] for row in measured if row[field] is not None]
            cells.append(f'{statistics.median(values):.4f}' if values else 'n/a')
        head.append(f'<tr><th>{html.escape(mode)}</th><td>{len(measured)} / {len(all_rows)}</td>'+
                    ''.join(f'<td>{cell}</td>' for cell in cells)+'</tr>')
    head.append('</tbody></table>')
    (output / 'report.html').write_text('\n'.join(head) + '\n')


def verify_requests(payload, expected):
    rows = payload['requests']
    if (len(rows)!=expected or {row['request'] for row in rows}!=set(range(expected))
            or payload['summary']['attempted']!=expected):
        raise ValueError('offered application request accounting disagrees')
    for row in rows:
        dispatch = row.get('dispatch_ns',row.get('start_ns'))
        if (not row['scheduled_ns']<=dispatch<=row['end_ns'] or
                row['outcome'] not in ('ok','failure','timeout','cancelled') or
                not math.isfinite(row['latency_ms']) or row['latency_ms']<0 or
                type(row['deadline_miss']) is not bool or row['outcome']!='ok' and not row['deadline_miss']):
            raise ValueError('invalid application request timing or outcome')
    completed = sum(row['outcome']=='ok' for row in rows)
    misses = sum(row['deadline_miss'] for row in rows)
    summary = payload['summary']
    if (summary['completed'],summary['errors'],summary['deadline_misses'])!=(completed,expected-completed,misses):
        raise ValueError('application summary disagrees with request records')


def paired_report(rows):
    complete = {(row['repetition'],row['mode']): row for row in rows if row['status']=='MEASURED'}
    result = {}
    for mode in sorted({row['mode'] for row in rows} - {'static','linux-default'}):
        pairs = [(candidate,complete[rep,'static']) for (rep,kind),candidate in complete.items()
                 if kind==mode and (rep,'static') in complete]
        metrics = {}
        for transport in ('http','grpc','http3','postgres'):
            for field in ('p99_ms','error_rate','deadline_miss_rate'):
                diffs = [left[transport][field]-right[transport][field] for left,right in pairs
                         if left.get(transport) and right.get(transport) and
                         left[transport][field] is not None and right[transport][field] is not None]
                interval = median_ci(diffs)
                metrics[f'{transport}.{field}'] = dict(pairs=len(diffs),
                    median_delta=statistics.median(diffs) if diffs else None,
                    interval=interval, classification=('DESCRIPTIVE_DIRECTION' if interval and
                    (interval['low']>0 or interval['high']<0) else 'INCONCLUSIVE'))
        result[mode] = metrics
    return dict(reference='static',effect='candidate minus reference',
                note='P99 is conditional on success. Report alongside failures. Intervals unadjusted.',
                comparisons=result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--modes', default=','.join(MODES))
    parser.add_argument('--stack', action='store_true', help='gateway/backend/PostgreSQL stack')
    parser.add_argument('--profile', choices=('stable','abundant','application','dynamic'), default='stable')
    parser.add_argument('--pool-size', type=int, default=8)
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
    parser.add_argument('--postgres-image', default=PINNED_POSTGRES)
    parser.add_argument('--grpc', action='store_true')
    parser.add_argument('--grpc-cancel-every', type=int, default=0)
    parser.add_argument('--grpc-rate', type=float, default=20)
    parser.add_argument('--grpc-concurrency', type=int, default=4)
    parser.add_argument('--grpc-deadline-ms', type=float, default=30)
    parser.add_argument('--http3-python', type=Path, help='project-local Python with config/quic-requirements.txt')
    parser.add_argument('--http3-rate', type=int, default=20)
    parser.add_argument('--http3-concurrency', type=int, default=4)
    parser.add_argument('--http3-deadline-ms', type=int, default=100)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    selected = args.modes.split(',')
    if args.stack:
        args.postgres = True
    if not 1 <= args.pool_size <= 32 or (not args.stack and args.profile != 'stable'):
        parser.error('profiles require --stack and a pool size of 1..32')
    if args.profile == 'dynamic' and (args.duration < 8 or 'linux-default' in selected):
        parser.error('dynamic requires at least 8 seconds and a shaped mode')
    if args.profile in ('abundant', 'application'):
        args.background_bytes = 0
    if args.postgres and not re.fullmatch(r'[a-z0-9./:_-]+@sha256:[0-9a-f]{64}',args.postgres_image):
        parser.error('PostgreSQL requires a locally available digest-pinned image')
    quic_version = None
    if args.http3_python:
        # Keep the venv launcher path. Resolving the symlink would bypass pyvenv.cfg.
        args.http3_python = args.http3_python.absolute()
        if not args.stack or not (1<=args.http3_rate<=100 and 1<=args.duration<=60 and
                1<=args.http3_concurrency<=16 and 1<=args.http3_deadline_ms<=2000):
            parser.error('HTTP/3 requires --stack and bounded client settings')
        probe = subprocess.run([args.http3_python,'-B','-c','import aioquic; print(aioquic.__version__)'],
                               capture_output=True,text=True,timeout=5)
        if probe.returncode or probe.stdout.strip() != '1.7.2':
            parser.error('install config/quic-requirements.txt in the HTTP/3 environment')
        quic_version = probe.stdout.strip()
    if args.grpc:
        probe = subprocess.run([sys.executable, '-B', APPS/'grpc_dependencies.py'],
                               capture_output=True, text=True, timeout=5)
        if probe.returncode:
            parser.error(probe.stderr.strip()[:300])
    if args.postgres:
        try:
            import psycopg  # noqa: F401
        except ImportError:
            parser.error('install examples/real_apps/requirements.txt for PostgreSQL workloads')
    if args.grpc and (
            not math.isfinite(args.grpc_rate) or not .1 <= args.grpc_rate <= 500 or
            not 1 <= args.grpc_concurrency <= 64 or
            args.grpc_concurrency > max(1, math.ceil(args.grpc_rate * args.duration)) or
            not 0 < args.grpc_deadline_ms <= 600000 or not 0 <= args.grpc_cancel_every <= 10000):
        parser.error('invalid gRPC workload parameters')
    if (not math.isfinite(args.rate) or not .1 <= args.rate <= 500 or
            not math.isfinite(args.deadline_ms) or not 0 < args.deadline_ms <= 600000 or
            not 1 <= args.concurrency <= 64 or
            args.concurrency > max(1, math.ceil(args.rate * args.duration)) or
            not 0 <= args.background_bytes <= 64 * 1024 * 1024):
        parser.error('invalid HTTP workload parameters')
    if args.postgres and (
            not math.isfinite(args.db_rate) or not .1 <= args.db_rate <= 500 or
            not 1 <= args.db_concurrency <= 64 or
            args.db_concurrency > max(1, math.ceil(args.db_rate * args.duration)) or
            not math.isfinite(args.db_deadline_ms) or not 0 < args.db_deadline_ms <= 600000):
        parser.error('invalid PostgreSQL workload parameters')
    if (not selected or any(mode not in (*MODES, *CONTROLLERS) for mode in selected) or len(set(selected)) != len(selected)
            or not 1 <= args.repetitions <= 30 or not 1 <= args.capacity_kbit <= 1000000
            or not 20 <= args.critical_min_pct <= 50 or not 1 <= args.duration <= 180
            or not 1 <= args.concurrency <= 64):
        parser.error('invalid experiment matrix')
    if os.geteuid() != 0:
        parser.error('isolated application experiments require the privileged research runner')
    RESULTS.mkdir(exist_ok=True)
    output = (args.output or RESULTS / f'real-applications-{time.time_ns()}').absolute()
    owned_directory(output)
    rng = random.Random(args.seed)
    trials = [(mode, repetition, seed) for repetition, seed in
              enumerate(rng.sample(range(1,2**32),args.repetitions))
              for mode in rng.sample(selected,len(selected))]
    before_host = host_identity()
    rows = []
    metadata_path = output / 'environment.json'
    save(metadata_path, {'host': metadata(), 'mode_order': trials,
                         'workload': vars(args) | {'output': str(output),
                                                    'http3_python': str(args.http3_python) if args.http3_python else None},
                         'aioquic_version': quic_version,
                         'source_sha256': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                           for path in [Path(__file__).resolve(), ROOT/'scripts'/'lab_tls.py',
                                                        *sorted(APPS.glob('*.py'))]},
                         'reproduction_argv': [sys.executable,'-B',*sys.argv],
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
    after_host = host_identity()
    save(output / 'host-safety.json', dict(unchanged=after_host==before_host))
    if after_host != before_host:
        raise RuntimeError('host link or qdisc fingerprint changed')
    save(output / 'results.json', rows)
    save(output / 'paired.json', paired_report(rows))
    print(json.dumps({'output': str(output), 'runs': len(rows),
                      'measured': sum(row.get('status') == 'MEASURED' for row in rows),
                      'invalid': sum(row.get('status') != 'MEASURED' for row in rows)}))
    return 0 if all(row.get('status') == 'MEASURED' for row in rows) else 2


if __name__ == '__main__':
    raise SystemExit(main())
